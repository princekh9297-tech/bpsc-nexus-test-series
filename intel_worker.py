"""Scheduled PIB RSS ingester. Run as a Render Cron Job: python intel_worker.py"""
import asyncio, hashlib, html, logging, os, re, json, urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import intel
logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'))
log=logging.getLogger('nexus_intel_worker')

def generate_ai_draft(title, source_url, source_text):
    """Optional OpenAI-compatible provider; returns None when not configured or invalid."""
    endpoint=os.getenv('INTEL_AI_BASE_URL','').strip().rstrip('/')
    key=os.getenv('INTEL_AI_API_KEY','').strip()
    model=os.getenv('INTEL_AI_MODEL','').strip()
    if not (endpoint and key and model): return None
    prompt=("Create an exam-oriented current-affairs draft from this official-source item. "
      "Do not invent facts; mark missing details as requiring verification. Return ONLY valid JSON with keys "
      "summary, background, key_facts (array of strings), significance, mains_points, bihar_relevance, mcqs (array). "
      "Each MCQ must contain question_en, question_hi, options object with A-D, correct_option, explanation_en, explanation_hi. "
      "Generate at most 3 MCQs. Preserve uncertainty and do not claim that unverified facts are official.\n"
      f"TITLE: {title}\nOFFICIAL URL: {source_url}\nSOURCE TEXT: {source_text[:12000]}")
    payload=json.dumps({'model':model,'temperature':0.2,'messages':[{'role':'system','content':'You are a careful exam-content editor. Never fabricate source facts.'},{'role':'user','content':prompt}],'response_format':{'type':'json_object'}}).encode()
    req=Request(endpoint+'/chat/completions',data=payload,headers={'Content-Type':'application/json','Authorization':'Bearer '+key})
    with urlopen(req,timeout=60) as response: data=json.loads(response.read().decode())
    content=data['choices'][0]['message']['content']
    result=json.loads(content)
    if not isinstance(result,dict) or not result.get('summary'): return None
    result['mcqs']=[q for q in result.get('mcqs',[]) if isinstance(q,dict) and q.get('correct_option') in ('A','B','C','D','E')][:3]
    return result

def parse_date(value):
    if not value: return None
    try: return parsedate_to_datetime(value).astimezone(timezone.utc)
    except Exception: return None

def clean(s): return re.sub(r'\s+',' ',html.unescape(re.sub('<[^>]+>',' ',s or ''))).strip()
def fetch_feed(url):
    req=Request(url,headers={'User-Agent':'BPSC-Nexus-Intel/1.0 (+official-source-monitor)'})
    with urlopen(req,timeout=25) as response: raw=response.read(5_000_000)
    root=ET.fromstring(raw); out=[]
    for item in root.findall('.//item'):
        get=lambda name: next((e.text or '' for e in item if e.tag.split('}')[-1].lower()==name.lower()),'')
        link=get('link').strip(); title=clean(get('title')); body=clean(get('description') or get('encoded'))
        if not link or not title: continue
        pub=parse_date(get('pubDate')); guid=get('guid').strip() or link
        out.append({'url':link,'title':title,'body':body,'published':pub,'guid':guid})
    # Atom compatibility
    if not out:
        ns={'a':'http://www.w3.org/2005/Atom'}
        for entry in root.findall('.//a:entry',ns):
            title=clean(entry.findtext('a:title',default='',namespaces=ns)); linkel=entry.find('a:link',ns)
            link=linkel.attrib.get('href','') if linkel is not None else ''
            body=clean(entry.findtext('a:summary',default='',namespaces=ns) or entry.findtext('a:content',default='',namespaces=ns))
            if link and title: out.append({'url':link,'title':title,'body':body,'published':parse_date(entry.findtext('a:published',default='',namespaces=ns)),'guid':link})
    return out

async def main():
    dsn=os.getenv('DATABASE_URL'); feed=os.getenv('PIB_FEED_URL','').strip()
    if not dsn: raise RuntimeError('DATABASE_URL is required')
    if not feed: raise RuntimeError('Set PIB_FEED_URL to the verified official PIB RSS feed URL before enabling ingestion')
    pool=AsyncConnectionPool(conninfo=dsn,min_size=1,max_size=2,open=False,kwargs={'row_factory':dict_row,'connect_timeout':10})
    await pool.open(); await pool.wait()
    try:
        await intel.ensure_schema(pool)
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT id FROM intel_sources WHERE source_code='pib' AND is_active=TRUE")
            source=await cur.fetchone()
            if not source:
                log.warning('PIB source is inactive. Set its feed URL and activate it after verifying the official endpoint.'); return
            sid=source['id']
        try: items=await asyncio.to_thread(fetch_feed,feed)
        except Exception as e:
            async with pool.connection() as conn: await conn.execute("UPDATE intel_sources SET last_checked_at=NOW(),last_error=%s,updated_at=NOW() WHERE id=%s",(str(e)[:1000],sid))
            raise
        new=0
        async with pool.connection() as conn:
            async with conn.transaction():
                for item in items:
                    body=item['body']; digest=hashlib.sha256((item['title']+'\n'+body).encode()).hexdigest()
                    cur=await conn.execute("""INSERT INTO intel_articles(source_id,external_id,canonical_url,title,published_at,raw_content,content_hash,processing_status)
                      VALUES(%s,%s,%s,%s,%s,%s,%s,'drafted') ON CONFLICT DO NOTHING RETURNING id""",(sid,item['guid'][:1000],item['url'],item['title'],item['published'],body,digest))
                    article=await cur.fetchone()
                    if not article: continue
                    aid=article['id']; new+=1
                    ai=None
                    try: ai=await asyncio.to_thread(generate_ai_draft,item['title'],item['url'],body)
                    except Exception: log.exception('AI draft generation failed for article %s',aid)
                    summary=(ai.get('summary') if ai else None) or (body[:1800] if body else 'Source item captured. Open the official link and prepare an editorial summary before approval.')
                    background=(ai.get('background') if ai else None) or 'Draft requires editorial review against the official source.'
                    facts=json.dumps(ai.get('key_facts',[]) if ai else [],ensure_ascii=False)
                    significance=(ai.get('significance') if ai else None) or 'Verify the official source and add exam significance before publication.'
                    mains=(ai.get('mains_points') if ai else None) or 'Add Mains relevance after editorial verification.'
                    bihar=(ai.get('bihar_relevance') if ai else None) or 'Assess Bihar relevance; do not assume a Bihar-specific connection.'
                    mcqs=json.dumps(ai.get('mcqs',[]) if ai else [],ensure_ascii=False)
                    await conn.execute("""INSERT INTO intel_content_versions(article_id,version_number,language,summary,background,key_facts,significance,mains_points,bihar_relevance,mcqs,generation_model,generation_status)
                      VALUES(%s,1,'en',%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s) ON CONFLICT DO NOTHING""",
                      (aid,summary,background,facts,significance,mains,bihar,mcqs,os.getenv('INTEL_AI_MODEL','source-excerpt'), 'draft' if ai else 'needs_ai_or_editor'))
                    await conn.execute("INSERT INTO intel_jobs(job_type,payload) VALUES('draft_article',jsonb_build_object('article_id',%s))",(aid,))
                await conn.execute("UPDATE intel_sources SET last_checked_at=NOW(),last_success_at=NOW(),last_error=NULL,updated_at=NOW() WHERE id=%s",(sid,))
        # Deliver approved Telegram publications with retry-safe state and a stable DB record.
        token=os.getenv('BOT_TOKEN',''); chat=os.getenv('INTEL_TELEGRAM_CHAT_ID','')
        if token and chat:
            async with pool.connection() as conn:
                cur=await conn.execute("""SELECT p.id,p.article_id,p.content_version_id,a.title,a.canonical_url,v.summary,v.significance
                  FROM intel_publications p JOIN intel_articles a ON a.id=p.article_id
                  JOIN intel_content_versions v ON v.id=p.content_version_id
                  WHERE p.destination='telegram' AND p.status IN ('queued','failed') AND a.review_status='approved'
                  ORDER BY p.id LIMIT 10 FOR UPDATE OF p SKIP LOCKED""")
                pending=await cur.fetchall()
            for pub in pending:
                msg=f"📡 <b>BPSC NEXUS INTEL</b>\n\n<b>{html.escape(pub['title'])}</b>\n\n{html.escape((pub['summary'] or '')[:2200])}\n\n<b>Exam significance</b>\n{html.escape((pub['significance'] or 'See the official source.')[:500])}\n\n<a href=\"{html.escape(pub['canonical_url'],quote=True)}\">Read official source</a>"
                try:
                    payload=json.dumps({'chat_id':chat,'text':msg,'parse_mode':'HTML','disable_web_page_preview':True}).encode()
                    req=urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage',data=payload,headers={'Content-Type':'application/json'})
                    response=await asyncio.to_thread(urllib.request.urlopen,req,timeout=20)
                    result=json.loads(response.read().decode())
                    if not result.get('ok'): raise RuntimeError(str(result)[:500])
                    message_id=result['result']['message_id']
                    async with pool.connection() as conn:
                        await conn.execute("UPDATE intel_publications SET status='published',telegram_chat_id=%s,telegram_message_id=%s,published_at=NOW(),attempts=attempts+1,last_error=NULL WHERE id=%s",(int(chat) if str(chat).lstrip('-').isdigit() else None,message_id,pub['id']))
                except Exception as e:
                    async with pool.connection() as conn:
                        await conn.execute("UPDATE intel_publications SET status='failed',attempts=attempts+1,last_error=%s WHERE id=%s",(str(e)[:1000],pub['id']))
                    log.exception('Telegram publication failed for article %s',pub['article_id'])
        log.info('PIB feed checked: %s items, %s new',len(items),new)
    finally: await pool.close()
if __name__=='__main__': asyncio.run(main())
