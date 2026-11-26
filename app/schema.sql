BEGIN;
CREATE TABLE IF NOT EXISTS offers (
 id TEXT PRIMARY KEY NOT NULL, group_id TEXT, name TEXT NOT NULL, vendor TEXT, vendor_code TEXT,
 url TEXT, price REAL, currency TEXT, available INTEGER NOT NULL DEFAULT 1, description TEXT,
 params_json TEXT NOT NULL DEFAULT '{}', is_russian_registry INTEGER NOT NULL DEFAULT 0,
 search_text TEXT, embedding BLOB
);
CREATE INDEX IF NOT EXISTS offers_group ON offers(group_id);
CREATE INDEX IF NOT EXISTS offers_vendor ON offers(vendor);
CREATE INDEX IF NOT EXISTS offers_vendor_code ON offers(vendor_code);
CREATE INDEX IF NOT EXISTS offers_vendor_code_nocase ON offers(vendor_code COLLATE NOCASE, id);
CREATE INDEX IF NOT EXISTS offers_registry ON offers(is_russian_registry);
CREATE VIRTUAL TABLE IF NOT EXISTS offers_fts USING fts5(offer_id UNINDEXED,name,vendor,vendor_code,description,search_text);
CREATE TRIGGER IF NOT EXISTS offers_ai AFTER INSERT ON offers BEGIN
 INSERT INTO offers_fts(rowid,offer_id,name,vendor,vendor_code,description,search_text)
 VALUES(new.rowid,new.id,new.name,new.vendor,new.vendor_code,new.description,new.search_text);
END;
CREATE TRIGGER IF NOT EXISTS offers_ad AFTER DELETE ON offers BEGIN
 DELETE FROM offers_fts WHERE rowid=old.rowid;
END;
DROP TRIGGER IF EXISTS offers_au;
CREATE TRIGGER offers_au AFTER UPDATE ON offers
WHEN old.rowid IS NOT new.rowid OR old.id IS NOT new.id OR old.name IS NOT new.name
 OR old.vendor IS NOT new.vendor OR old.vendor_code IS NOT new.vendor_code
 OR old.description IS NOT new.description OR old.search_text IS NOT new.search_text
BEGIN
 DELETE FROM offers_fts WHERE rowid=old.rowid;
 INSERT INTO offers_fts(rowid,offer_id,name,vendor,vendor_code,description,search_text)
 VALUES(new.rowid,new.id,new.name,new.vendor,new.vendor_code,new.description,new.search_text);
END;
CREATE TABLE IF NOT EXISTS kb_articles (
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, url TEXT, content TEXT NOT NULL,
 source TEXT NOT NULL CHECK(source IN ('docs','delivery','manager_added')), embedding BLOB,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(article_id UNINDEXED,title,content);
CREATE TRIGGER IF NOT EXISTS kb_ai AFTER INSERT ON kb_articles BEGIN
 INSERT INTO kb_fts(rowid,article_id,title,content) VALUES(new.id,new.id,new.title,new.content);
END;
CREATE TRIGGER IF NOT EXISTS kb_ad AFTER DELETE ON kb_articles BEGIN
 DELETE FROM kb_fts WHERE rowid=old.id;
END;
DROP TRIGGER IF EXISTS kb_au;
CREATE TRIGGER kb_au AFTER UPDATE ON kb_articles
WHEN old.id IS NOT new.id OR old.title IS NOT new.title OR old.content IS NOT new.content
BEGIN
 DELETE FROM kb_fts WHERE rowid=old.id;
 INSERT INTO kb_fts(rowid,article_id,title,content) VALUES(new.id,new.id,new.title,new.content);
END;
CREATE TABLE IF NOT EXISTS chats (
 chat_id TEXT PRIMARY KEY NOT NULL, page_url TEXT,
 status TEXT NOT NULL DEFAULT 'bot' CHECK(status IN ('bot','escalated','manager','closed')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL REFERENCES chats(chat_id),
 sender TEXT NOT NULL CHECK(sender IN ('client','bot','manager','system')),
 kind TEXT NOT NULL CHECK(kind IN ('visitor','operator','file_operator')), text TEXT, file_url TEXT,
 meta_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id,id);
CREATE TABLE IF NOT EXISTS escalations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL REFERENCES chats(chat_id),
 reason TEXT NOT NULL, summary TEXT NOT NULL, collected_slots_json TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS quotes (
 id TEXT PRIMARY KEY NOT NULL, chat_id TEXT NOT NULL REFERENCES chats(chat_id), items_json TEXT NOT NULL,
 discount_percent REAL NOT NULL CHECK(discount_percent BETWEEN 0 AND 5), total_sum REAL NOT NULL,
 pdf_url TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 total_sum_kopecks INTEGER CHECK(total_sum_kopecks IS NULL OR (typeof(total_sum_kopecks)='integer'
  AND total_sum_kopecks BETWEEN 1 AND 1000000000000))
);
CREATE TABLE IF NOT EXISTS escalation_redirects (
 chat_id TEXT PRIMARY KEY NOT NULL REFERENCES chats(chat_id),
 status TEXT NOT NULL CHECK(status IN ('pending','sent'))
);
CREATE TABLE IF NOT EXISTS quote_confirmations (
 chat_id TEXT PRIMARY KEY NOT NULL REFERENCES chats(chat_id), confirmation_id TEXT NOT NULL UNIQUE,
 proposal_json TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1))
);
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, event_type TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}', prompt_tokens INTEGER, completion_tokens INTEGER,
 cost_usd REAL, latency_ms INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS audit_type_chat ON audit_log(event_type,chat_id);
CREATE TABLE IF NOT EXISTS webim_visitors (
 chat_id TEXT PRIMARY KEY NOT NULL REFERENCES chats(chat_id), visitor_fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webim_empty_assignments (
 chat_id TEXT PRIMARY KEY NOT NULL REFERENCES chats(chat_id)
);
CREATE TABLE IF NOT EXISTS webim_inbox (
 seq INTEGER PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(chat_id),
 message_id TEXT NOT NULL UNIQUE, pending_text TEXT, pending_page_url TEXT
);
CREATE INDEX IF NOT EXISTS webim_inbox_pending ON webim_inbox(chat_id,seq) WHERE pending_text IS NOT NULL;
CREATE TABLE IF NOT EXISTS demo_chats (
 chat_id TEXT PRIMARY KEY REFERENCES chats(chat_id), capability_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_kb_drafts (
 id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES demo_chats(chat_id),
 reply_id INTEGER NOT NULL REFERENCES messages(id), question TEXT NOT NULL, answer TEXT NOT NULL,
 mode TEXT NOT NULL, article_id INTEGER REFERENCES kb_articles(id)
);
COMMIT;
