"""BPSC Nexus Intel: additive schema and admin review controls."""
from __future__ import annotations
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
log = logging.getLogger('bpsc_nexus_intel')

DDL = [
"""CREATE TABLE IF NOT EXISTS intel_sources (
 id BIGSERIAL PRIMARY KEY, source_code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 source_type TEXT NOT NULL DEFAULT 'rss', feed_url TEXT NOT NULL, is_active BOOLEAN NOT NULL DEFAULT TRUE,
 poll_interval_minutes INTEGER NOT NULL DEFAULT 20, last_checked_at TIMESTAMPTZ, last_success_at TIMESTAMPTZ,
 last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
"""CREATE TABLE IF NOT EXISTS intel_articles (
 id BIGSERIAL PRIMARY KEY, source_id BIGINT NOT NULL REFERENCES intel_sources(id) ON DELETE RESTRICT,
 external_id TEXT, canonical_url TEXT NOT NULL, title TEXT NOT NULL, published_at TIMESTAMPTZ,
 author TEXT, raw_content TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'en',
 processing_status TEXT NOT NULL DEFAULT 'new' CHECK (processing_status IN ('new','queued','drafted','filtered','failed')),
 review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending','approved','rejected')),
 relevance_score NUMERIC(5,2), discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 approved_at TIMESTAMPTZ, approved_by BIGINT, UNIQUE(source_id, content_hash), UNIQUE(source_id, canonical_url))""",
"CREATE INDEX IF NOT EXISTS intel_articles_review_idx ON intel_articles(review_status, discovered_at DESC)",
"CREATE INDEX IF NOT EXISTS intel_articles_pub_idx ON intel_articles(published_at DESC)",
"""CREATE TABLE IF NOT EXISTS intel_content_versions (
 id BIGSERIAL PRIMARY KEY, article_id BIGINT NOT NULL REFERENCES intel_articles(id) ON DELETE RESTRICT,
 version_number INTEGER NOT NULL, language TEXT NOT NULL DEFAULT 'en', summary TEXT NOT NULL DEFAULT '',
 background TEXT NOT NULL DEFAULT '', key_facts JSONB NOT NULL DEFAULT '[]'::jsonb,
 significance TEXT NOT NULL DEFAULT '', mains_points TEXT NOT NULL DEFAULT '', bihar_relevance TEXT NOT NULL DEFAULT '',
 mcqs JSONB NOT NULL DEFAULT '[]'::jsonb, generation_model TEXT, generation_status TEXT NOT NULL DEFAULT 'draft',
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), created_by TEXT NOT NULL DEFAULT 'system', UNIQUE(article_id, version_number, language))""",
"""CREATE TABLE IF NOT EXISTS intel_reviews (
 id BIGSERIAL PRIMARY KEY, article_id BIGINT NOT NULL REFERENCES intel_articles(id) ON DELETE RESTRICT,
 content_version_id BIGINT REFERENCES intel_content_versions(id) ON DELETE RESTRICT,
 reviewer_telegram_id BIGINT NOT NULL, action TEXT NOT NULL CHECK(action IN ('approved','rejected','edited','regenerated')),
 notes TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
"""CREATE TABLE IF NOT EXISTS intel_publications (
 id BIGSERIAL PRIMARY KEY, article_id BIGINT NOT NULL REFERENCES intel_articles(id) ON DELETE RESTRICT,
 content_version_id BIGINT NOT NULL REFERENCES intel_content_versions(id) ON DELETE RESTRICT,
 destination TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'queued'
 CHECK(status IN ('queued','publishing','published','failed')), telegram_chat_id BIGINT, telegram_message_id BIGINT,
 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), published_at TIMESTAMPTZ,
 UNIQUE(article_id, content_version_id, destination))""",
"""CREATE TABLE IF NOT EXISTS intel_jobs (
 id BIGSERIAL PRIMARY KEY, job_type TEXT NOT NULL, payload JSONB NOT NULL DEFAULT '{}'::jsonb,
 status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','done','failed')),
 attempt_count INTEGER NOT NULL DEFAULT 0, available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), locked_at TIMESTAMPTZ,
 last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), completed_at TIMESTAMPTZ)""",
"CREATE INDEX IF NOT EXISTS intel_jobs_ready_idx ON intel_jobs(status, available_at, id)",
"""CREATE TABLE IF NOT EXISTS intel_categories (
 id BIGSERIAL PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL, is_active BOOLEAN NOT NULL DEFAULT TRUE)""",
"""CREATE TABLE IF NOT EXISTS intel_article_categories (
 article_id BIGINT NOT NULL REFERENCES intel_articles(id) ON DELETE CASCADE,
 category_id BIGINT NOT NULL REFERENCES intel_categories(id) ON DELETE RESTRICT,
 relevance_score NUMERIC(5,2), PRIMARY KEY(article_id,category_id))""",
"""CREATE TABLE IF NOT EXISTS intel_bookmarks (
 telegram_id BIGINT NOT NULL, article_id BIGINT NOT NULL REFERENCES intel_articles(id) ON DELETE CASCADE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(telegram_id,article_id))""",
"""CREATE TABLE IF NOT EXISTS intel_read_status (
 telegram_id BIGINT NOT NULL, article_id BIGINT NOT NULL REFERENCES intel_articles(id) ON DELETE CASCADE,
 read_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(telegram_id,article_id))""",
"""CREATE TABLE IF NOT EXISTS intel_feedback (
 id BIGSERIAL PRIMARY KEY, telegram_id BIGINT NOT NULL, article_id BIGINT NOT NULL REFERENCES intel_articles(id) ON DELETE CASCADE,
 feedback_type TEXT NOT NULL, details TEXT, status TEXT NOT NULL DEFAULT 'open', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
"""CREATE TABLE IF NOT EXISTS intel_audit_log (
 id BIGSERIAL PRIMARY KEY, actor_telegram_id BIGINT, action TEXT NOT NULL, entity_type TEXT NOT NULL,
 entity_id BIGINT, details JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
"""INSERT INTO intel_categories(slug,name) VALUES
 ('polity','Polity & Governance'),('economy','Economy'),('science-tech','Science & Technology'),
 ('environment','Environment'),('schemes','Government Schemes'),('international','International Relations'),
 ('bihar','Bihar Special'),('awards','Awards & Reports'),('other','Other') ON CONFLICT(slug) DO NOTHING"""
]

async def ensure_schema(pool):
    async with pool.connection() as conn:
        for sql in DDL:
            await conn.execute(sql)
        feed_url = __import__('os').environ.get('PIB_FEED_URL','').strip()
        if feed_url:
            await conn.execute("""INSERT INTO intel_sources(source_code,name,source_type,feed_url)
              VALUES('pib','Press Information Bureau','rss',%s) ON CONFLICT(source_code) DO UPDATE SET feed_url=EXCLUDED.feed_url,is_active=TRUE,updated_at=NOW()""",(feed_url,))
        await conn.execute("INSERT INTO intel_sources(source_code,name,source_type,feed_url,is_active) VALUES('pib','Press Information Bureau','rss','https://pib.gov.in/RssMain.aspx',FALSE) ON CONFLICT(source_code) DO NOTHING")

async def show_home(query, pool):
    async with pool.connection() as conn:
        cur=await conn.execute("""SELECT COUNT(*) FILTER(WHERE review_status='pending') AS pending,
          COUNT(*) FILTER(WHERE review_status='approved') AS approved,
          COUNT(*) FILTER(WHERE processing_status='failed') AS failed FROM intel_articles""")
        row=await cur.fetchone()
    kb=InlineKeyboardMarkup([
      [InlineKeyboardButton('📝 Review queue',callback_data='intel:queue:0')],
      [InlineKeyboardButton('📊 Pipeline status',callback_data='intel:status')],
      [InlineKeyboardButton('⬅ Admin Panel',callback_data='adm:home')]])
    await query.edit_message_text('📡 BPSC NEXUS INTEL\n\nPIB → AI notes & MCQs → admin approval → students\n\n'
      f'⏳ Pending review: {row["pending"]}\n✅ Approved: {row["approved"]}\n⚠️ Processing failures: {row["failed"]}\n\n'
      'Only explicitly approved versions can be published.',reply_markup=kb)

async def route_callback(update, context, pool, admin_only):
    q=update.callback_query; data=q.data or ''
    if not data.startswith('intel:'): return False
    if not admin_only(update):
        await q.answer('Not authorized.',show_alert=True); return True
    await q.answer()
    if data=='intel:home':
        await show_home(q,pool); return True
    if data=='intel:status':
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT source_code,name,is_active,last_checked_at,last_success_at,last_error FROM intel_sources ORDER BY id")
            rows=await cur.fetchall()
            cur=await conn.execute("SELECT status,COUNT(*) AS n FROM intel_jobs GROUP BY status")
            jobs=await cur.fetchall()
        lines=['📊 Nexus Intel status','']
        for s in rows: lines.append(f"{'🟢' if s['is_active'] else '⚪'} {s['name']} · {'active' if s['is_active'] else 'inactive'} · last check: {s['last_checked_at'] or 'never'}"+(f"\nError: {s['last_error'][:160]}" if s['last_error'] else ''))
        lines += ['', 'Jobs: '+(', '.join(f"{x['status']}={x['n']}" for x in jobs) if jobs else 'none')]
        await q.edit_message_text('\n'.join(lines),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅ Intel',callback_data='intel:home')]])); return True
    if data.startswith('intel:queue:'):
        offset=max(0,int(data.split(':')[-1]));
        async with pool.connection() as conn:
            cur=await conn.execute("SELECT id,title,published_at,discovered_at FROM intel_articles WHERE review_status='pending' AND processing_status IN ('drafted','new','queued') ORDER BY discovered_at DESC LIMIT 8 OFFSET %s",(offset,)); rows=await cur.fetchall()
        if not rows:
            await q.edit_message_text('📭 No pending Intel articles.\n\nThe worker must be configured and run to ingest PIB items.',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↻ Refresh',callback_data='intel:queue:0')],[InlineKeyboardButton('⬅ Intel',callback_data='intel:home')]])); return True
        buttons=[]
        for a in rows: buttons.append([InlineKeyboardButton(f"#{a['id']} · {a['title'][:52]}",callback_data=f"intel:article:{a['id']}")])
        nav=[]
        if offset: nav.append(InlineKeyboardButton('← Previous',callback_data=f'intel:queue:{max(0,offset-8)}'))
        if len(rows)==8: nav.append(InlineKeyboardButton('Next →',callback_data=f'intel:queue:{offset+8}'))
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton('⬅ Intel',callback_data='intel:home')])
        await q.edit_message_text('📝 Intel Review Queue\nSelect an article to inspect its source and draft.',reply_markup=InlineKeyboardMarkup(buttons)); return True
    if data.startswith('intel:article:'):
        aid=int(data.split(':')[-1])
        async with pool.connection() as conn:
            cur=await conn.execute("""SELECT a.*,s.name AS source_name,v.id AS version_id,v.summary,v.background,v.key_facts,v.significance,v.mains_points,v.bihar_relevance,v.mcqs
             FROM intel_articles a JOIN intel_sources s ON s.id=a.source_id LEFT JOIN LATERAL
             (SELECT * FROM intel_content_versions WHERE article_id=a.id ORDER BY version_number DESC LIMIT 1) v ON TRUE WHERE a.id=%s""",(aid,)); a=await cur.fetchone()
        if not a: await q.edit_message_text('Article not found.'); return True
        text=f"📡 INTEL #{aid}\n{a['title']}\n\nSource: {a['source_name']}\nURL: {a['canonical_url']}\nPublished: {a['published_at'] or 'Unknown'}\nStatus: {a['processing_status']} / {a['review_status']}\n\nSUMMARY\n{a['summary'] or a['raw_content'][:1200] or 'No draft yet.'}"
        if a['background']: text+='\n\nBACKGROUND\n'+a['background'][:700]
        if a['key_facts']: text+='\n\nKEY FACTS\n'+'\n'.join('• '+str(x) for x in a['key_facts'][:6])
        if a['significance']: text+='\n\nEXAM SIGNIFICANCE\n'+a['significance'][:500]
        if a['mains_points']: text+='\n\nMAINS POINTS\n'+a['mains_points'][:500]
        buttons=[]
        if a['version_id'] and a['processing_status']=='drafted' and a['review_status']=='pending':
            buttons=[[InlineKeyboardButton('✅ Approve version',callback_data=f'intel:approve:{aid}:{a["version_id"]}'),InlineKeyboardButton('❌ Reject',callback_data=f'intel:reject:{aid}:{a["version_id"]}')]]
        buttons.append([InlineKeyboardButton('⬅ Queue',callback_data='intel:queue:0')])
        await q.edit_message_text(text[:3900],reply_markup=InlineKeyboardMarkup(buttons),disable_web_page_preview=True); return True
    if data.startswith('intel:approve:') or data.startswith('intel:reject:'):
        parts=data.split(':'); action=parts[1]; aid=int(parts[2]); vid=int(parts[3]); actor=update.effective_user.id
        async with pool.connection() as conn:
            async with conn.transaction():
                cur=await conn.execute("SELECT id FROM intel_content_versions WHERE id=%s AND article_id=%s",(vid,aid)); ver=await cur.fetchone()
                cur=await conn.execute("SELECT review_status FROM intel_articles WHERE id=%s FOR UPDATE",(aid,)); art=await cur.fetchone()
                if not ver or not art or art['review_status']!='pending':
                    await q.edit_message_text('This draft has changed or was already reviewed. Refresh the queue.'); return True
                approved=action=='approve'
                await conn.execute("UPDATE intel_articles SET review_status=%s,processing_status=%s,approved_at=CASE WHEN %s THEN NOW() ELSE approved_at END,approved_by=CASE WHEN %s THEN %s ELSE approved_by END,updated_at=NOW() WHERE id=%s",('approved' if approved else 'rejected','drafted' if approved else 'filtered',approved,approved,actor,aid))
                await conn.execute("INSERT INTO intel_reviews(article_id,content_version_id,reviewer_telegram_id,action) VALUES(%s,%s,%s,%s)",(aid,vid,actor,'approved' if approved else 'rejected'))
                await conn.execute("INSERT INTO intel_audit_log(actor_telegram_id,action,entity_type,entity_id) VALUES(%s,%s,'article',%s)",(actor,'intel_'+action,aid))
                if approved:
                    await conn.execute("INSERT INTO intel_publications(article_id,content_version_id,destination,idempotency_key,status) VALUES(%s,%s,'telegram','intel:%s:v%s:telegram','queued') ON CONFLICT DO NOTHING",(aid,vid,aid,vid))
                    await conn.execute("INSERT INTO intel_publications(article_id,content_version_id,destination,idempotency_key,status) VALUES(%s,%s,'miniapp','intel:%s:v%s:miniapp','published') ON CONFLICT DO NOTHING",(aid,vid,aid,vid))
        await q.edit_message_text(('✅ Approved' if approved else '❌ Rejected')+f' article #{aid}. '+('Telegram publication has been queued; Mini App API visibility is enabled.' if approved else 'This item will not be published.'),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Review next',callback_data='intel:queue:0')],[InlineKeyboardButton('Intel home',callback_data='intel:home')]])); return True
    return True
