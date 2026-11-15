# Cedar Harbour — restricted operations handover

Owner: Maya Rowan, platform operations. Approver: Elias North, incident coordinator.
This runbook belongs to the internal Cedar Harbour programme and is shared only
with the operations team. Product names such as Python, Docker, PostgreSQL and Redis
are public technology names; the deployment identifiers below describe our private stack.

## Service map

The ingest service harbour-ingest runs on ingest01.harbour.test at 10.93.27.16.
The settlement service harbour-settle runs on settle01.harbour.test at 10.93.27.17.
The project namespace is harbour_ops and its deployment repository is harbour-deploy.
The on-call operator signs in as rowan-demo; scheduled jobs use svc_harbour_batch.
Application state is written to /srv/harbour/state and reports to /srv/harbour/reports.
The company-wide private domain for this deployment is harbour.test.

## Access material for the maintenance window

The temporary console password is Birch!71Harbour#906. The importer API key is
kpn_fixture_import_J6m4R8p2T9w7C3a5. A separate archive uses the passphrase
River-Archive!38-Maple. These values are independent credentials; do not join the
password, API key and archive phrase into a single replacement.

The last console password, Birch!70Harbour#805, remains in an earlier log export
and still needs masking when that historical export is shared. The new password
is Birch!71Harbour#906. A successful rotation does not make an old password safe
to expose in a conversation or erase its mapping from previously processed text.

## Maintenance steps

1. Notify Maya Rowan at maya.rowan@harbour.test and copy oncall@harbour.test.
2. Drain harbour-ingest, confirm the queue stops growing, and record the HTTP status.
3. Take a local snapshot of /srv/harbour/state and verify that its manifest is readable.
4. Restart harbour-settle, check ingest01.harbour.test and settle01.harbour.test,
   then resume the queue gradually. Avoid increasing concurrency before checking memory.
5. Use rowan-demo to run the acceptance request. Stop if authentication fails twice.
6. Remove temporary access after the window and record the outcome in OPS-HBR-2026-031.

The first request may legitimately return HTTP 429 during recovery. The retry policy
allows three attempts with a two-second delay. Values 200, 429, 502, 3 and 2 in this
paragraph describe behaviour, not financial terms or credentials.

## Customer context

The private customer code is CL-HBR-028. The customer's legal name in this handover
is Aster Quay Research Ltd. Commercial values and customer identities are shared
company-wide only with authorized staff. The operational names above belong to the
project team; named staff and their login credentials belong to personal scope.
The monthly reserved capacity costs USD 18,450.00 and the separately agreed emergency
allowance is USD 2,750.00. These are private prices, not public catalogue examples.

## Safe troubleshooting excerpts

Use https://docs.python.org/3/ for the language reference and
https://www.postgresql.org/docs/ for public database documentation. The public model
name gemini-3.8-flash may appear in a client configuration and is not our project.
The words character and questions are ordinary JSON field names in the request sample.
A masked password such as *** or an existing token ANON_SECRET_19 is not a new secret.
The fragments e::, ::C and ::f come from source-code examples and do not identify hosts.
