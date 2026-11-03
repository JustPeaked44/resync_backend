"""Local-only migration 012 check. Uses the pinned PostgreSQL binaries and synthetic rows."""
import csv
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(r"D:\RESYNC_DEV\handoff\session\db")
BIN = Path(r"D:\RESYNC_DEV\_tools\pgsql\bin")
DATA = Path(r"D:\RESYNC_DEV\_tools\pgdata")
DB = "resync_012_test"
ENV = {**os.environ, "PGHOST": "127.0.0.1", "PGPORT": "54329", "PGUSER": "postgres", "PGPASSWORD": ""}


def run(args, *, check=True):
    ctl_start = Path(args[0]).name.lower() == 'pg_ctl.exe' and 'start' in args
    result = subprocess.run([str(a) for a in args], env=ENV, text=True,
                            stdout=subprocess.DEVNULL if ctl_start else subprocess.PIPE,
                            stderr=subprocess.DEVNULL if ctl_start else subprocess.PIPE)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(map(str, args))}\n{result.stdout[-1200:]}\n{result.stderr[-2400:]}")
    return result


def sql(query, db=DB):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".sql", delete=False) as f:
        f.write(query)
        name = f.name
    try:
        return run([BIN / "psql.exe", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-d", db, "-f", name]).stdout.strip()
    finally:
        Path(name).unlink()


def file(path):
    return run([BIN / "psql.exe", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-d", DB, "-f", path]).stdout.strip()


def sql_error(query, message):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".sql", delete=False) as f:
        f.write(query)
        name = f.name
    try:
        result = run([BIN / "psql.exe", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-d", DB, "-f", name], check=False)
        check(message, (result.returncode != 0, message in result.stderr), (True, True))
    finally:
        Path(name).unlink()


def check(label, got, expected):
    good = got == expected
    print(f"{'PASS' if good else 'FAIL'} {label}: actual={got!r} expected={expected!r}", flush=True)
    if not good:
        raise AssertionError(label)


def catalog():
    return json.loads(sql("""SELECT jsonb_object_agg(table_name, cols) FROM
      (SELECT c.table_name,jsonb_object_agg(c.column_name,jsonb_build_object(
        'type',CASE WHEN c.udt_name='bool' THEN 'bool' WHEN c.udt_name='int4' THEN 'int4'
          WHEN c.udt_name='float4' THEN 'float4' WHEN c.udt_name='float8' THEN 'float8'
          WHEN c.udt_name='timestamptz' THEN 'timestamptz' WHEN c.data_type='character varying' THEN 'varchar'
          WHEN c.data_type='numeric' THEN 'numeric' ELSE c.udt_name END,
        'nullable',c.is_nullable='YES')) cols
       FROM information_schema.columns c WHERE c.table_schema='public' GROUP BY c.table_name) s;"""))


def signature():
    result = {
        'columns': json.loads(sql("""SELECT jsonb_object_agg(table_name||'.'||column_name,
          jsonb_build_array(data_type,udt_name,is_nullable,column_default))
          FROM information_schema.columns WHERE table_schema='public';""")),
        'constraints': json.loads(sql("""SELECT coalesce(jsonb_object_agg(t.relname||'.'||c.conname,
          pg_get_constraintdef(c.oid)),'{}'::jsonb) FROM pg_constraint c
          JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
          WHERE n.nspname='public';""")),
        'functions': json.loads(sql("""SELECT jsonb_object_agg(p.proname,pg_get_functiondef(p.oid))
          FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public';""")),
        'triggers': json.loads(sql("""SELECT jsonb_object_agg(t.tgname,pg_get_triggerdef(t.oid))
          FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='auth' AND NOT t.tgisinternal;""")),
        'rls': json.loads(sql("""SELECT jsonb_object_agg(relname,relrowsecurity) FROM pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r';""")),
        'policies': json.loads(sql("""SELECT coalesce(jsonb_object_agg(tablename||'.'||policyname,
          jsonb_build_array(cmd,qual,with_check,roles)),'{}'::jsonb) FROM pg_policies WHERE schemaname='public';""")),
        'grants': json.loads(sql("""SELECT jsonb_object_agg(c.relname,
          (SELECT jsonb_agg(x::text ORDER BY x::text) FROM unnest(c.relacl) x))
          FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname='public' AND c.relkind='r';""")),
        'indexes': json.loads(sql("""SELECT jsonb_object_agg(indexname,indexdef)
          FROM pg_indexes WHERE schemaname='public';""")),
        'enums': json.loads(sql("""SELECT jsonb_object_agg(t.typname,
          (SELECT jsonb_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid=t.oid))
          FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public' AND t.typtype='e';""")),
    }
    result['functions'] = {name: body.replace('\r', '') for name, body in result['functions'].items()}
    return result


def assert_schema(spec):
    actual = catalog()
    check("A tables", sorted(actual), sorted(spec["tables"]))
    defaults = json.loads(sql("""SELECT coalesce(jsonb_object_agg(c.relname||'.'||a.attname,
      pg_get_expr(d.adbin,d.adrelid)), '{}'::jsonb) FROM pg_attrdef d
      JOIN pg_attribute a ON a.attrelid=d.adrelid AND a.attnum=d.adnum
      JOIN pg_class c ON c.oid=d.adrelid JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='public';"""))
    constraints = json.loads(sql("""SELECT coalesce(jsonb_agg(jsonb_build_object('table',t.relname,'name',c.conname,
      'kind',c.contype,'column',a.attname,'target',tn.nspname||'.'||tt.relname||'.'||ta.attname,
      'delete',c.confdeltype) ORDER BY t.relname,c.conname),'[]'::jsonb)
      FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
      JOIN pg_namespace n ON n.oid=t.relnamespace
      LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=c.conkey[1]
      LEFT JOIN pg_class tt ON tt.oid=c.confrelid
      LEFT JOIN pg_namespace tn ON tn.oid=tt.relnamespace
      LEFT JOIN pg_attribute ta ON ta.attrelid=tt.oid AND ta.attnum=c.confkey[1]
      WHERE n.nspname='public' AND c.contype IN ('p','f','u');"""))
    rls = json.loads(sql("""SELECT jsonb_object_agg(relname,relrowsecurity) FROM pg_class c
      JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND relkind='r';"""))
    status_check = sql("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
      WHERE conrelid='public.analysis_run'::regclass AND conname='analysis_run_status_check';""")
    check("A status allowed values", re.findall(r"'([^']+)'", status_check),
          spec['tables']['analysis_run']['analysis_run_status']['allowed_values'])
    for table, columns in spec["tables"].items():
        check(f"A columns {table}", sorted(actual[table]), sorted(columns))
        check(f"A RLS {table}", rls[table], True)
        for name, value in columns.items():
            check(f"A {table}.{name} type/nullable", actual[table][name],
                  {"type": value["type"], "nullable": value["nullable"]})
            matches = [c for c in constraints if c['table'] == table and c['column'] == name]
            check(f"A PK {table}.{name}", sum(c['kind']=='p' for c in matches), int(value['pk']))
            fks = [c for c in matches if c['kind']=='f']
            check(f"A FK {table}.{name}", [(c['target'],c['delete']) for c in fks],
                  [((value['fk'] if value['fk'].startswith('auth.') else 'public.' + value['fk']),
                    {'cascade':'c','set null':'n','no action':'a'}.get(value['on_delete']))] if value['fk'] else [])
            unique = name in spec['unique_constraints'].get(table, [])
            check(f"A unique {table}.{name}", sum(c['kind']=='u' for c in matches), int(unique))
            if value['default'] is not None:
                actual_default = defaults.get(table + '.' + name)
                expected = value['default'].replace('::priority_enum','::public.priority_enum')
                normalize = lambda s: s.replace('::public.', '::').replace('::text','').replace('::character varying','') if s else s
                check(f"A default {table}.{name}", normalize(actual_default), normalize(expected))


SEED = r"""
-- The live CHECK rejects 'complete'; relax it only in this synthetic fixture to exercise migration normalization.
ALTER TABLE public.analysis_run DROP CONSTRAINT analysis_run_status_check;
INSERT INTO public.subscription_plan(plan_name) VALUES ('Free');
INSERT INTO auth.users(id,email,raw_user_meta_data) VALUES
('00000000-0000-0000-0000-000000000001','a@example.test','{"first_name":"A"}'),
('00000000-0000-0000-0000-000000000002','b@example.test','{}'),
('00000000-0000-0000-0000-000000000003','c@example.test','{}'),
('00000000-0000-0000-0000-000000000004','d@example.test','{}');
INSERT INTO public.manuscript(manuscript_id,user_id,manuscript_title,manuscript_source_url) VALUES
('10000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000001','Paper A','https://example.test/a'),
('10000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000002','Paper B','https://example.test/b');
INSERT INTO public.analysis_run(analysis_run_id,manuscript_id,user_id,analysis_run_status,status,result_json,analysis_run_template_toc) VALUES
('20000000-0000-0000-0000-000000000001','10000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000001','processing','processing',NULL,NULL),
('20000000-0000-0000-0000-000000000002','10000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000001','completed','complete','{"access":{"unlocked":false},"answer":1}','["Intro"]'),
('20000000-0000-0000-0000-000000000003','10000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000001','completed',NULL,'{"answer":2}',''),
('20000000-0000-0000-0000-000000000004','10000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000002','failed','failed','{"access":{"unlocked":true}}',NULL);
INSERT INTO public.inconsistency(inconsistency_id,analysis_run_id,inconsistency_type,inconsistency_severity,inconsistency_description,inconsistency_explanation_what,inconsistency_explanation_why,inconsistency_suggested_fix,primary_section_name) VALUES
('30000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000002','structural','High','x','what','why','fix','Intro'),
('30000000-0000-0000-0000-000000000002','20000000-0000-0000-0000-000000000003','contextual','Low','y','what','why','fix','Methods');
INSERT INTO public.issue_feedback(inconsistency_id,user_id,is_helpful,created_at) VALUES
('30000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000001',true,'2025-01-01'),
('30000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000001',false,'2025-01-02');
INSERT INTO public.recommendation(analysis_run_id,recommendation_text) VALUES ('20000000-0000-0000-0000-000000000002','Fix it');
INSERT INTO public.citation(analysis_run_id,citation_raw_reference_text) VALUES ('20000000-0000-0000-0000-000000000002','Reference');
INSERT INTO public.citation_crossmatch_issue(analysis_run_id,issue_type) VALUES ('20000000-0000-0000-0000-000000000002','orphan_intext');
INSERT INTO public.notification(user_id,notification_type,notification_payload) VALUES ('00000000-0000-0000-0000-000000000001','scan','{}');
INSERT INTO public.user_device_token(user_id,device_onesignal_player_id,registered_at) VALUES
('00000000-0000-0000-0000-000000000001','dupe','2025-01-01'),
('00000000-0000-0000-0000-000000000001','dupe','2025-01-02');
UPDATE public.credit_wallet SET balance=3 WHERE user_id='00000000-0000-0000-0000-000000000001';
UPDATE public.credit_wallet SET balance=0 WHERE user_id='00000000-0000-0000-0000-000000000002';
INSERT INTO public.pymt_txn(subscription_id,pymt_txn_amount) SELECT subscription_id,10 FROM public.subscription WHERE user_id='00000000-0000-0000-0000-000000000001';
"""


def main():
    started = False
    try:
        result = run([BIN / "pg_ctl.exe", "-D", DATA, "-l", DATA / "server.log", "-w", "start"], check=False)
        if result.returncode:
            raise RuntimeError(f'pg_ctl start failed: {result.returncode}')
        started = result.returncode == 0
        sql(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE); CREATE DATABASE {DB};", "postgres")
        for role in ("anon", "authenticated", "service_role", "supabase_admin"):
            sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN CREATE ROLE {role} NOLOGIN; END IF; END $$;")
        sql("""DROP SCHEMA public CASCADE; CREATE SCHEMA auth;
          CREATE TABLE auth.users(id uuid PRIMARY KEY,email text,raw_user_meta_data jsonb);
          CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT NULL::uuid $$;""")
        print("LOAD live_schema.sql", file(SOURCE / "live_schema.sql")[-100:])
        with (SOURCE / "db_functions.csv").open(newline="", encoding="utf-8") as f:
            defs = list(csv.DictReader(f))
        for item in defs:
            if item["kind"] == "function":
                sql(item["definition"] + ";")
        for item in defs:
            if item["kind"] == "trigger":
                sql(item["definition"] + ";")
        check("before tables", len(catalog()), 15)
        before = signature()
        sql(SEED)
        print("RUN 012", file(ROOT / "migrations" / "012_schema_cleanup.sql")[-100:])
        spec = json.loads((SOURCE / "build" / "final_schema.json").read_text())
        assert_schema(spec)
        check("B wallet balances", sql("""SELECT count(*) FROM backup_012.credit_wallet w
          WHERE w.balance=public.get_credit_balance(w.user_id)"""), "4")
        check("B latest vote", sql("SELECT inconsistency_is_helpful::text FROM public.inconsistency WHERE inconsistency_id='30000000-0000-0000-0000-000000000001'"), "false")
        check("B statuses", sql("SELECT string_agg(analysis_run_status,',' ORDER BY analysis_run_id) FROM public.analysis_run"), "processing,completed,completed,failed")
        check("B unlock flags", sql("SELECT string_agg(analysis_run_is_unlocked::text,',' ORDER BY analysis_run_id) FROM public.analysis_run"), "false,false,true,true")
        check("B template", sql("SELECT analysis_run_template_toc::text FROM public.analysis_run WHERE analysis_run_id='20000000-0000-0000-0000-000000000002'"), '["Intro"]')
        check("B payment owner", sql("SELECT count(*) FROM public.pymt_txn WHERE user_id='00000000-0000-0000-0000-000000000001'"), "1")
        check("B kept user/manuscript/run/inconsistency/recommendation/citation counts",
              sql("SELECT (SELECT count(*) FROM public.\"user\"),(SELECT count(*) FROM public.manuscript),(SELECT count(*) FROM public.analysis_run),(SELECT count(*) FROM public.inconsistency),(SELECT count(*) FROM public.recommendation),(SELECT count(*) FROM public.citation)"),
              '4|2|4|2|1|1')
        check("B device dedupe", sql("SELECT count(*) FROM public.user_device_token"), "1")
        check("B opening adjustments", sql("SELECT count(*) FROM public.credit_ledger WHERE credit_ledger_kind='adjustment'"), "2")
        sql("INSERT INTO auth.users(id,email,raw_user_meta_data) VALUES ('00000000-0000-0000-0000-000000000005','e@example.test','{}')")
        check("C signup profile/grant/balance", sql("SELECT (SELECT count(*) FROM public.\"user\" WHERE user_id='00000000-0000-0000-0000-000000000005'),(SELECT count(*) FROM public.credit_ledger WHERE user_id='00000000-0000-0000-0000-000000000005' AND credit_ledger_kind='grant'),public.get_credit_balance('00000000-0000-0000-0000-000000000005')"), "1|1|1")
        check("C debit 1 to 0", sql("SELECT public.debit_scan_credit('00000000-0000-0000-0000-000000000005',NULL,'c-debit')"), "0")
        sql_error("SELECT public.debit_scan_credit('00000000-0000-0000-0000-000000000005',NULL,'c-debit-2')", "INSUFFICIENT_CREDITS")
        check("C refund", sql("SELECT public.refund_scan_credit('00000000-0000-0000-0000-000000000005',NULL,'c-refund')"), "1")
        check("C idempotent debit twice", [sql("SELECT public.debit_scan_credit('00000000-0000-0000-0000-000000000005',NULL,'c-idem')") for _ in range(2)], ["0", "0"])
        check("C one idempotent row", sql("SELECT count(*) FROM public.credit_ledger WHERE credit_ledger_idempotency_key='c-idem'"), "1")
        sql("INSERT INTO public.pymt_txn(user_id,pymt_txn_amount) VALUES ('00000000-0000-0000-0000-000000000005',20)")
        check("C purchase +2", sql("SELECT public.purchase_credits('00000000-0000-0000-0000-000000000005',2,(SELECT pymt_txn_id FROM public.pymt_txn WHERE user_id='00000000-0000-0000-0000-000000000005'),'c-buy')"), "2")
        check("C purchase payment link", sql("SELECT count(*) FROM public.credit_ledger l JOIN public.pymt_txn p USING (pymt_txn_id) WHERE l.credit_ledger_idempotency_key='c-buy' AND p.user_id=l.user_id"), "1")
        sql_error("SELECT public.purchase_credits('00000000-0000-0000-0000-000000000005',0,NULL,'c-bad')", "INVALID_AMOUNT")
        sql_error("SELECT public.unlock_scan_report('00000000-0000-0000-0000-000000000002','20000000-0000-0000-0000-000000000002',true)", "SCAN_NOT_FOUND")
        sql_error("SELECT public.unlock_scan_report('00000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000001',true)", "SCAN_NOT_READY")
        before_json = sql("SELECT analysis_run_result_json::text FROM public.analysis_run WHERE analysis_run_id='20000000-0000-0000-0000-000000000002'")
        check("C unlock charge", json.loads(sql("SELECT public.unlock_scan_report('00000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000002',true)")), {"unlocked":True,"charged":True,"credits_remaining":2})
        check("C unlock again", json.loads(sql("SELECT public.unlock_scan_report('00000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000002',true)")), {"unlocked":True,"charged":False,"credits_remaining":2})
        check("C JSON byte-identical", sql("SELECT analysis_run_result_json::text FROM public.analysis_run WHERE analysis_run_id='20000000-0000-0000-0000-000000000002'"), before_json)
        check("C unlocked flag", sql("SELECT analysis_run_is_unlocked::text FROM public.analysis_run WHERE analysis_run_id='20000000-0000-0000-0000-000000000002'"), "true")
        def debit(key):
            return run([BIN / "psql.exe", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-d", DB,
                        "-c", f"SELECT public.debit_scan_credit('00000000-0000-0000-0000-000000000003',NULL,'{key}')"], check=False)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(debit, ('parallel-a', 'parallel-b')))
        check("D parallel outcomes", sorted((r.returncode == 0, 'INSUFFICIENT_CREDITS' in r.stderr) for r in results), [(False, True),(True, False)])
        check("D final balance", sql("SELECT public.get_credit_balance('00000000-0000-0000-0000-000000000003')"), "0")
        sql("""INSERT INTO public.manuscript(manuscript_id,user_id,manuscript_title,manuscript_source_url,manuscript_content_hash)
          VALUES ('10000000-0000-0000-0000-000000000005','00000000-0000-0000-0000-000000000005','New paper','https://example.test/new','hash-new');
          INSERT INTO public.analysis_run(analysis_run_id,manuscript_id,user_id,analysis_run_status,analysis_run_result_json)
          VALUES ('20000000-0000-0000-0000-000000000005','10000000-0000-0000-0000-000000000005','00000000-0000-0000-0000-000000000005','completed','{"access":{"unlocked":false}}');
          INSERT INTO public.inconsistency(inconsistency_id,analysis_run_id,inconsistency_type,inconsistency_severity,
            inconsistency_explanation_what,inconsistency_explanation_why,inconsistency_suggested_fix,inconsistency_section_a,inconsistency_is_helpful)
          VALUES ('30000000-0000-0000-0000-000000000005','20000000-0000-0000-0000-000000000005','structural','High','new what','new why','new fix','Intro',true);
          INSERT INTO public.recommendation(analysis_run_id,recommendation_text) VALUES ('20000000-0000-0000-0000-000000000005','New fix');
          INSERT INTO public.citation(analysis_run_id,citation_raw_reference_text) VALUES ('20000000-0000-0000-0000-000000000005','New reference');
          INSERT INTO public.user_device_token(user_id,user_device_token_player_id) VALUES ('00000000-0000-0000-0000-000000000005','new-device');
          INSERT INTO public.pair_verdict_cache(pair_verdict_cache_key,pair_verdict_cache_prompt_version,pair_verdict_cache_explanation)
          VALUES ('pair-new','v1','{}');""")
        sql("UPDATE public.inconsistency SET inconsistency_is_helpful=true WHERE inconsistency_id='30000000-0000-0000-0000-000000000001'")
        print("RUN UNDO", file(ROOT / "migrations" / "012_schema_cleanup_undo.sql")[-100:])
        after = signature()
        for part in ('columns', 'constraints', 'functions', 'triggers', 'rls', 'policies', 'grants', 'indexes', 'enums'):
            different = {key: (before[part].get(key), after[part].get(key))
                         for key in before[part].keys() | after[part].keys()
                         if before[part].get(key) != after[part].get(key)}
            check(f"E schema {part} differences", different, {})
        check("E old tables and new rows", sql("SELECT (SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'),(SELECT count(*) FROM public.\"user\"),(SELECT count(*) FROM public.pymt_txn),(SELECT count(*) FROM public.user_device_token),(SELECT count(*) FROM public.notification)"), "15|5|2|3|1")
        check("E current wallet balances", sql("SELECT string_agg(balance::text,',' ORDER BY user_id) FROM public.credit_wallet"), "2,0,0,1,2")
        check("E old and new rows retained", sql("SELECT (SELECT count(*) FROM public.manuscript),(SELECT count(*) FROM public.analysis_run),(SELECT count(*) FROM public.inconsistency),(SELECT count(*) FROM public.recommendation),(SELECT count(*) FROM public.citation),(SELECT count(*) FROM public.issue_feedback),(SELECT count(*) FROM public.citation_crossmatch_issue),(SELECT count(*) FROM public.credit_ledger)"), "3|5|3|2|2|4|1|11")
        check("E original dropped-column values", sql("SELECT manuscript_research_type||'|'||manuscript_upload_mode||'|'||manuscript_status FROM public.manuscript WHERE manuscript_id='10000000-0000-0000-0000-000000000001'"), "Quantitative|Google Docs Link|active")
        check("E old enum values", sql("SELECT string_agg(enumlabel,',' ORDER BY enumsortorder) FROM pg_enum WHERE enumtypid='public.credit_ledger_kind'::regtype"), "grant,purchase,debit,refund")
        check("E sign-up row mapped back", sql("SELECT (SELECT count(*) FROM public.subscription WHERE user_id='00000000-0000-0000-0000-000000000005'),(SELECT count(*) FROM public.credit_wallet WHERE user_id='00000000-0000-0000-0000-000000000005' AND balance=2)"), "1|1")
        check("E new finding/feedback mapped back", sql("SELECT i.inconsistency_description||'|'||f.is_helpful::text FROM public.inconsistency i JOIN public.issue_feedback f USING (inconsistency_id) WHERE i.inconsistency_id='30000000-0000-0000-0000-000000000005'"), "new what|true")
        check("E changed vote mapped back", sql("SELECT is_helpful::text FROM public.issue_feedback WHERE inconsistency_id='30000000-0000-0000-0000-000000000001' ORDER BY created_at DESC,feedback_id DESC LIMIT 1"), "true")
        check("E new-only fields retained in backup", sql("SELECT (SELECT count(*) FROM backup_012.undo_pair_verdict_cache WHERE pair_verdict_cache_key='pair-new'),(SELECT value->>'manuscript_content_hash' FROM backup_012.undo_new_fields WHERE source_table='manuscript' AND row_id='10000000-0000-0000-0000-000000000005')"), "1|hash-new")
        sql("DROP SCHEMA backup_012 CASCADE")
        print("RUN 012 AGAIN", file(ROOT / "migrations" / "012_schema_cleanup.sql")[-100:])
        assert_schema(spec)
        check("E rerun wallets match", sql("SELECT count(*) FROM backup_012.credit_wallet w WHERE w.balance=public.get_credit_balance(w.user_id)"), "5")
        check("E rerun B statuses", sql("SELECT string_agg(analysis_run_status,',' ORDER BY analysis_run_id) FROM public.analysis_run"), "processing,completed,completed,failed,completed")
        check("E rerun B unlock flags", sql("SELECT string_agg(analysis_run_is_unlocked::text,',' ORDER BY analysis_run_id) FROM public.analysis_run"), "false,true,true,true,false")
        check("E rerun B vote", sql("SELECT inconsistency_is_helpful::text FROM public.inconsistency WHERE inconsistency_id='30000000-0000-0000-0000-000000000001'"), "true")
        check("E rerun B counts", sql("SELECT (SELECT count(*) FROM public.\"user\"),(SELECT count(*) FROM public.manuscript),(SELECT count(*) FROM public.analysis_run),(SELECT count(*) FROM public.inconsistency),(SELECT count(*) FROM public.recommendation),(SELECT count(*) FROM public.citation),(SELECT count(*) FROM public.pymt_txn),(SELECT count(*) FROM public.user_device_token)"), "5|3|5|3|2|2|2|2")
        print("A-E COMPLETE")
    finally:
        if started:
            print("STOP PostgreSQL", run([BIN / "pg_ctl.exe", "-D", DATA, "-l", DATA / "server.log", "-w", "stop"], check=False).returncode)


if __name__ == "__main__":
    main()
