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
DB = "resync_f55_017_test"
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


def editor_run(path):
    """Send the whole file as ONE request, the way the Supabase SQL editor does (not split by psql)."""
    return run([BIN / "psql.exe", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-d", DB, "-c",
                Path(path).read_text(encoding="utf-8")]).stdout.strip()


def migrate_012():
    editor_run(ROOT / "migrations" / "017a_ledger_kind.sql")
    return editor_run(ROOT / "migrations" / "017_schema_cleanup.sql")


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
    proof_check = sql("""SELECT pg_get_constraintdef(oid) FROM pg_constraint
      WHERE conrelid='public.pymt_txn'::regclass AND conname='pymt_txn_proof_method_check';""")
    check("A proof method allowed values", re.findall(r"'([^']+)'", proof_check),
          spec['tables']['pymt_txn']['pymt_txn_proof_method']['allowed_values'])
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
('20000000-0000-0000-0000-000000000001','10000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000001','failed','failed',NULL,NULL),
('20000000-0000-0000-0000-000000000002','10000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000001','completed','completed','{"access":{"unlocked":false},"answer":1}','["Intro"]'),
('20000000-0000-0000-0000-000000000003','10000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000001','completed',NULL,'{"answer":2}',''),
('20000000-0000-0000-0000-000000000004','10000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000002','failed','failed','{"access":{"unlocked":true}}',NULL);
UPDATE public.analysis_run SET error_message='This report was deleted when it was closed. Scan the paper again to get a new report.' WHERE analysis_run_id='20000000-0000-0000-0000-000000000001';
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
('00000000-0000-0000-0000-000000000001','device2','2025-01-02');
UPDATE public.credit_wallet SET balance=3 WHERE user_id='00000000-0000-0000-0000-000000000001';
UPDATE public.credit_wallet SET balance=0 WHERE user_id='00000000-0000-0000-0000-000000000002';
INSERT INTO public.pymt_txn(user_id,pymt_txn_amount,pymt_txn_status) VALUES ('00000000-0000-0000-0000-000000000001',10,'pending'),('00000000-0000-0000-0000-000000000001',20,'paid');
"""


def main():
    started = False
    created = False
    try:
        ready = run([BIN / "pg_isready.exe", "-h", "127.0.0.1", "-p", "54329"], check=False)
        if ready.returncode:
            result = run([BIN / "pg_ctl.exe", "-D", DATA, "-l", DATA / "server.log", "-w", "start"], check=False)
            if result.returncode:
                raise RuntimeError(f"pg_ctl start failed: {result.returncode}")
            started = True
        if sql(f"SELECT count(*) FROM pg_database WHERE datname='{DB}'", "postgres") != "0":
            raise RuntimeError(f"Refusing to overwrite existing database {DB}")
        sql(f"CREATE DATABASE {DB}", "postgres")
        created = True
        for role in ("anon", "authenticated", "service_role", "supabase_admin"):
            sql(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN CREATE ROLE {role} NOLOGIN; END IF; END $$;")
        sql("""DROP SCHEMA public CASCADE; CREATE SCHEMA auth;
          CREATE TABLE auth.users(id uuid PRIMARY KEY,email text,raw_user_meta_data jsonb);
          CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT NULL::uuid $$;""")
        file(SOURCE / "live_schema.sql")
        with (SOURCE / "db_functions.csv").open(newline="", encoding="utf-8") as f:
            defs = list(csv.DictReader(f))
        for kind in ("function", "trigger"):
            for item in defs:
                if item["kind"] == kind:
                    sql(item["definition"] + ";")
        for name in ("012_payment_proof.sql", "013_email_registered.sql", "014_pymt_txn_user.sql", "015_new_accounts_start_empty.sql", "016_lock_credit_functions.sql"):
            file(ROOT / "migrations" / name)
        sql(SEED)
        before = signature()
        check("read-only precheck inventory rows", len(file(ROOT / "migrations" / "017_precheck_readonly.sql").splitlines()), 30)
        counts_sql = "SELECT (SELECT count(*) FROM public.\"user\"),(SELECT count(*) FROM public.manuscript),(SELECT count(*) FROM public.analysis_run),(SELECT count(*) FROM public.inconsistency),(SELECT count(*) FROM public.recommendation),(SELECT count(*) FROM public.citation),(SELECT count(*) FROM public.pymt_txn),(SELECT count(*) FROM public.user_device_token)"
        counts_before = sql(counts_sql)
        sql("DROP FUNCTION public.email_registered(text)")
        invalid_before = signature()
        rejected = run([BIN / "psql.exe", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-d", DB, "-c", (ROOT / "migrations" / "017_schema_cleanup.sql").read_text(encoding="utf-8")], check=False)
        check("missing function precondition rejects without changes", (rejected.returncode != 0, "017 precondition" in rejected.stderr, signature() == invalid_before), (True, True, True))
        file(ROOT / "migrations" / "013_email_registered.sql")
        check("initial zero credit", sql("SELECT balance FROM public.credit_wallet WHERE user_id='00000000-0000-0000-0000-000000000004'"), "0")
        unprepared = signature()
        skipped = run([BIN / "psql.exe", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-d", DB, "-c", (ROOT / "migrations" / "017_schema_cleanup.sql").read_text(encoding="utf-8")], check=False)
        check("enum must run separately first", (skipped.returncode != 0, "017a_ledger_kind.sql first" in skipped.stderr, signature() == unprepared), (True, True, True))
        editor_run(ROOT / "migrations" / "017a_ledger_kind.sql")
        editor_run(ROOT / "migrations" / "017_schema_cleanup.sql")
        sql("""CREATE SCHEMA storage; CREATE TABLE storage.buckets(id text PRIMARY KEY,name text,public boolean);
          CREATE TABLE storage.objects(bucket_id text,name text);
          CREATE FUNCTION storage.foldername(text) RETURNS text[] LANGUAGE sql IMMUTABLE AS $$ SELECT string_to_array($1,'/') $$;""")
        editor_run(ROOT / "migrations" / "018_avatars_storage.sql")
        check("proof bucket is private", sql("SELECT public::text FROM storage.buckets WHERE id='payment-proofs'"), "false")
        check("no browser proof policies", sql("SELECT count(*) FROM pg_policies WHERE schemaname='storage' AND (qual LIKE '%payment-proofs%' OR with_check LIKE '%payment-proofs%')"), "0")
        spec = json.loads((SOURCE / "build" / "final_schema.json").read_text(encoding="utf-8"))
        assert_schema(spec)
        check("discarded report preserved", sql("SELECT analysis_run_status,analysis_run_result_json IS NULL,analysis_run_error_message LIKE 'This report was deleted%' FROM public.analysis_run WHERE analysis_run_id='20000000-0000-0000-0000-000000000001'"), "failed|t|t")
        check("all security definer privileges", sql("""SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname='public' AND p.prosecdef AND (has_function_privilege('anon',p.oid,'execute') OR has_function_privilege('authenticated',p.oid,'execute') OR NOT has_function_privilege('service_role',p.oid,'execute'))"""), "0")
        check("core row counts retained", sql(counts_sql), counts_before)
        check("locked and unlocked scans retained", sql("SELECT string_agg(analysis_run_is_unlocked::text,',' ORDER BY analysis_run_id) FROM public.analysis_run"), "false,false,true,true")
        check("latest feedback retained", sql("SELECT inconsistency_is_helpful::text FROM public.inconsistency WHERE inconsistency_id='30000000-0000-0000-0000-000000000001'"), "false")
        check("email lookup", sql("SELECT public.email_registered(' A@example.test '), public.email_registered('missing@example.test')"), "t|f")
        sql("INSERT INTO auth.users(id,email,raw_user_meta_data) VALUES ('00000000-0000-0000-0000-000000000005','new@example.test','{}')")
        check("new account starts at zero", sql("SELECT public.get_credit_balance('00000000-0000-0000-0000-000000000005')"), "0")
        check("payments retained", sql("SELECT string_agg(pymt_txn_status,',' ORDER BY pymt_txn_status) FROM public.pymt_txn"), "paid,pending")
        check("balances retained", sql("SELECT string_agg(public.get_credit_balance(user_id)::text,',' ORDER BY user_id) FROM public.\"user\""), "3,0,0,0,0")
        check("devices retained", sql("SELECT count(*) FROM public.user_device_token"), "2")
        file(ROOT / "migrations" / "017_schema_cleanup_undo.sql")
        check("undo discarded report preserved", sql("SELECT status,result_json IS NULL,error_message LIKE 'This report was deleted%' FROM public.analysis_run WHERE analysis_run_id='20000000-0000-0000-0000-000000000001'"), "failed|t|t")
        after = signature()
        for part in ('columns','constraints','functions','triggers','rls','policies','grants','indexes','enums'):
            different = {key: (before[part].get(key), after[part].get(key)) for key in before[part].keys() | after[part].keys() if before[part].get(key) != after[part].get(key)}
            check(f"undo schema {part}", different, {})
        check("undo callable privileges", sql("""SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname='public' AND p.proname IN ('email_registered','purchase_credits','debit_scan_credit','refund_scan_credit','unlock_scan_report')
          AND (has_function_privilege('anon',p.oid,'execute') OR has_function_privilege('authenticated',p.oid,'execute') OR NOT has_function_privilege('service_role',p.oid,'execute'))"""), "0")
        check("undo core counts", sql(counts_sql), counts_before.replace("4|", "5|", 1))
        check("undo feedback", sql("SELECT is_helpful::text FROM public.issue_feedback WHERE inconsistency_id='30000000-0000-0000-0000-000000000001' ORDER BY created_at DESC,feedback_id DESC LIMIT 1"), "false")
        check("undo balances", sql("SELECT string_agg(balance::text,',' ORDER BY user_id) FROM public.credit_wallet"), "3,0,0,0,0")
        check("undo payments", sql("SELECT string_agg(pymt_txn_status,',' ORDER BY pymt_txn_status) FROM public.pymt_txn"), "paid,pending")
        sql("DROP SCHEMA backup_012 CASCADE")
        file(ROOT / "migrations" / "011_pair_verdict_cache.sql")
        sql("INSERT INTO public.pair_verdict_cache(cache_key,prompt_version,explanation) VALUES ('synthetic-cache','v1','{}')")
        editor_run(ROOT / "migrations" / "017a_ledger_kind.sql")
        editor_run(ROOT / "migrations" / "017_schema_cleanup.sql")
        assert_schema(spec)
        check("discarded report preserved", sql("SELECT analysis_run_status,analysis_run_result_json IS NULL,analysis_run_error_message LIKE 'This report was deleted%' FROM public.analysis_run WHERE analysis_run_id='20000000-0000-0000-0000-000000000001'"), "failed|t|t")
        check("cached verdict preserved", sql("SELECT count(*) FROM public.pair_verdict_cache WHERE pair_verdict_cache_key='synthetic-cache'"), "1")
        first_unlock = json.loads(sql("SELECT public.unlock_scan_report('00000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000002',true)"))
        second_unlock = json.loads(sql("SELECT public.unlock_scan_report('00000000-0000-0000-0000-000000000001','20000000-0000-0000-0000-000000000002',true)"))
        check("unlock charges once", (first_unlock['charged'], second_unlock['charged'], second_unlock['credits_remaining']), (True, False, 2))
        file(ROOT / "migrations" / "017_schema_cleanup_undo.sql")
        check("charged balance survives undo", sql("SELECT balance FROM public.credit_wallet WHERE user_id='00000000-0000-0000-0000-000000000001'"), "2")
        check("cached verdict restored", sql("SELECT count(*) FROM public.pair_verdict_cache WHERE cache_key='synthetic-cache'"), "1")
        sql("DROP SCHEMA backup_012 CASCADE")
        sql("INSERT INTO public.payment_proof(pymt_txn_id,user_id,method,reference_number,content_type,proof_data) SELECT pymt_txn_id,user_id,'gcash','synthetic','image/png','synthetic' FROM public.pymt_txn LIMIT 1")
        editor_run(ROOT / "migrations" / "017a_ledger_kind.sql")
        proof_before = signature()
        rejected = run([BIN / "psql.exe", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-d", DB, "-c", (ROOT / "migrations" / "017_schema_cleanup.sql").read_text(encoding="utf-8")], check=False)
        check("existing proof rejected unchanged", (rejected.returncode != 0, "payment_proof has 1 rows" in rejected.stderr, signature() == proof_before, sql("SELECT count(*) FROM public.payment_proof")), (True, True, True, "1"))
        print("MIGRATE / UNDO / RE-APPLY COMPLETE")
    finally:
        if created:
            sql(f"DROP DATABASE {DB} WITH (FORCE)", "postgres")
        if started:
            print("STOP PostgreSQL", run([BIN / "pg_ctl.exe", "-D", DATA, "-l", DATA / "server.log", "-w", "stop"], check=False).returncode)


if __name__ == "__main__":
    main()
