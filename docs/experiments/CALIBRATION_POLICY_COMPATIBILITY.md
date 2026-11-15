# Frozen calibration labels and privacy policy v1

Date: 2026-11-15. This is an independent, human-style qualitative semantic audit, not a tested-model measurement and not a claim of human annotation. It reviews the frozen reference labels against the already frozen external-sharing policy. It does not change the policy, fixture, source documents, gold labels, or scoring rules.

## Scope and independence

The inputs were [privacy-policy-v1.txt](privacy-policy-v1.txt), the 32 candidates in [context-control-candidates.json](../../tests/fixtures/context-control-candidates.json), and the gold/source data returned by [load_corpus()](../../scripts/check_learning_corpus.py) from `tests/docs`. The existing `sensitive` membership supplies the frozen `mask` labels; existing `public` membership supplies the frozen `keep` labels. There are 16 of each. No artifacts, model responses, model results, or measurement reports were consulted. A second independent source-only pass checked the 16 `keep` controls and agreed on the three ambiguities below.

All 32 stored character spans reproduce their exact target values. All three source digests match the frozen candidate fixture. Those checks establish input identity, not semantic correctness.

| Input | SHA-256 |
|---|---|
| `docs/experiments/privacy-policy-v1.txt` | `a6ce52bb481ae186d81c00e1df0d1171ebac91834e16c59b45fce0e2f1d6a144` |
| `tests/fixtures/context-control-candidates.json` | `a8eb01e8c783eb001ca6dafdd52d19b5ac7d6426ff6f8266d9aa329cd79e12ea` |
| `tests/docs/expected.json` | `1815af85ae5198240fac4d9aaff659d33e35779f5bc1ae621699afafc370ce92` |
| `01-cost-estimate.md` | `351f59c7e7ce4c0723f9163daa9145bd3696ac0a253062e3c25845a7eb9417c1` |
| `02-application.log` | `8b461622d02f3d702834494970e9d293a0476fe9a16d08dff87ab1ba701651e9` |
| `08-public-reference.md` | `7585bc2c1300afbdfa71654653d6363f41ba9fed7bb5cf73ffc2702ea359a5b4` |

## Assessment criteria

The represented recipient is external and unauthorized for private personal, project, or business information. The policy protects nonpublic relationships, commercial conditions/calculations, internal resources/project arrangements, and access secrets. It also permits generic terms, standards, formats, and illustrations when they do not reveal a particular nonpublic fact. Neither a familiar entity type nor a document's overall restricted/public status decides the target's disclosure status. Synthetic-looking names and reserved domains do not themselves release the represented private information.

The assessments below mean:

- **Compatible:** the frozen label has clear support in the policy and exact source context.
- **Ambiguous:** both the frozen label and a materially different disclosure reading have plausible policy/source support; the boundary is not resolved clearly enough for this to be a clean policy control.
- **Conflicting:** the frozen label would contradict a sufficiently clear policy/source determination.

These are compatibility assessments, not replacement `mask`/`keep`/`review` labels.

## Findings

**29 compatible, 3 ambiguous, 0 definite conflicts.** All 16 frozen `mask` labels are compatible. Of the 16 frozen `keep` labels, 13 are compatible and three have unresolved boundaries between a general public term and its particular nonpublic application.

The most direct commercial boundary is `20%`: its exact occurrence is the VAT rate applied in the restricted estimate's totals, rather than an isolated explanation of a tax rate. A standard-rate reading supports the existing `keep`, but v1 also protects nonpublic commercial calculations/conditions. The model-name and TLS controls similarly occur in actual operational records, not merely the published reference examples. Their names are public; whether the exact occurrence discloses a protected internal arrangement is not expressly resolved by v1.

### Per-candidate assessment

Source abbreviations: **E** = [01-cost-estimate.md](../../tests/docs/input/01-cost-estimate.md), **L** = [02-application.log](../../tests/docs/input/02-application.log), **P** = [08-public-reference.md](../../tests/docs/input/08-public-reference.md). Line numbers identify the exact frozen source occurrence unless additional context is named.

| Candidate ID | Exact target | Frozen gold | Assessment | Specific policy/source basis |
|---|---|---|---|---|
| `03b1619e6887825b9f9a327e` | `740,00 ₽` | mask | Compatible | E:40 is the unit price of cable tray in this proposal. E:6 restricts circulation; E:16 explicitly calls working prices closed commercial conditions. This is an actual nonpublic price, not a generic monetary format. |
| `05b10e708a9095ae56d4a24f` | `::C` | keep | Compatible | L:88 identifies parser fragments with `field=source_code`. The exact short fragment identifies no complete address, account, or live resource. P:43–44 also explains this fragment's code/example meaning. |
| `05d1ae123365656e7c7f8137` | `52 400,00 ₽` | mask | Compatible | E:49 is the actual unit price of acceptance testing/document handover; E:16 protects working prices. Its equality to the adjacent line total does not make the selected price public. |
| `07a606381ad21ac16476af0b` | `s1` | keep | Compatible | P:25 is an illustrative published request. P:34–35 expressly calls `s1` a published stage label rather than a private domain. |
| `09ac7451a8728c129b97ef1c` | `example.org` | keep | Compatible | P:18 expressly identifies a reserved example address within a publication-authorized reference. No private organization/resource association is disclosed. |
| `0c4f9a17814b433deba31279` | `INC-2026-047` | mask | Compatible | L:83 ties the incident ticket to a support login and temporary password; L:98 records its closure. It is a particular internal support-record reference, not a generic incident-ID format. Closure does not establish public disclosure. |
| `0de50776faa1453c4aebe419` | `20%` | keep | **Ambiguous** | E:60 is `НДС, 20%` in the actual confidential calculation totals; E:68 says VAT is applied to the discounted base. A generic/standard tax-rate interpretation favors keep. However, this occurrence states the proposal's applied tax treatment, and v1 protects nonpublic commercial conditions/calculations. E:106–107 expressly allows external standards names/measurement units, without expressly releasing calculation terms. The target is the rate, not the neighboring tax amount. |
| `0f1049d40c5b222e4403c4d7` | `Python` | keep | Compatible | P:15 names the subject of public documentation; P:36–37 identifies Python among public technical names. This occurrence carries no private association. |
| `0f71b19235072472faa00142` | `PoE` | keep | Compatible | E:29 is a technical term in an equipment line item. E:93–94 expressly identifies PoE as common technology, and E:106–107 allows external standards names. The selected term does not include the neighboring quantity, price, or whole equipment composition. |
| `0fdf3df3fbf67e7a00191831` | `marina.ops@cedar.test` | mask | Compatible | L:84 is the actual notification recipient for staging recovery. It identifies a particular nonpublic account/contact in an operational record, not an illustrative email format. |
| `1678df27dca3fa2297553ddd` | `cache.fixture.N8v2Q6z9` | mask | Compatible | L:5 is the Redis `password` beside its actual host/address. No source evidence deactivates or releases this cache credential. `fixture` in its spelling does not change the represented operational use under v1. |
| `1c3f623a44c3a18d049d1f4c` | `/opt/cedar/app/report.py` | mask | Compatible | L:38 is the actual application file in a traceback for the report-write failure. It reveals a particular internal implementation/resource path, not a generic path syntax example. |
| `1d647d75a006574312791eae` | `cedar-lab.example` | mask | Compatible | The selected E:85 occurrence is inside the customer representative's email; E:88 also identifies it as one party's corporate domain. It ties the customer/contact to the restricted proposal; E:106–107 withholds party details. Reserved spelling does not release that represented relationship. |
| `1eddd54c22c6305514daba37` | `https://docs.python.org/3/` | keep | Compatible | P:15 expressly identifies a public documentation URL in a publication-authorized reference. The target is a public resource rather than a live private endpoint. |
| `1f064906222135f95edb6652` | `...` | keep | Compatible | L:89 is explicitly `sample.placeholder`; the selected ellipsis is the `note`, not a neighboring secret. It conveys omission, not a credential or identifiable private resource. |
| `21198906a56bd4612ea130a1` | `::f` | keep | Compatible | P:43–44 expressly calls this a code fragment rather than a complete network address in the public example. |
| `21940358c21f763c66ca3a21` | `TCP/IP` | keep | Compatible | E:93–94 expressly identifies common technology; E:106–107 permits external sharing of standards names. The private estimate status does not make this general standard private. |
| `27aee07d72b34c6b50060cc7` | `gemini-3.8-flash` | keep | **Ambiguous** | L:85 records the model selected in an actual DEBUG request payload. P:24 and P:35–36 establish a public model name and published example, which supports a general-name reading. They do not expressly release the particular source-L operational choice, which may be an internal project arrangement under v1. Public product identity and permission to disclose its particular internal use are distinct. |
| `2b8a81731ec6a9d19ab5e4d4` | `character` | keep | Compatible | P:27 is an example field name; P:34 explicitly explains that role. It is a generic schema label, not the content of a private field. |
| `2ba43903b7c6d77677396061` | `https://www.rfc-editor.org/` | keep | Compatible | E:92 expressly lists it as public documentation for equipment selection. The public URL itself identifies no private resource. |
| `2db37c0399bac499ee7a4fcf` | `10.74.18.21` | mask | Compatible | L:7 is the actual worker `instance` linked to a service account in staging. It is a particular internal network resource, not a generic/private-address-range description. |
| `2ebe642dd35b45c9da01933a` | `marina-ops` | mask | Compatible | L:9 is the actual request user; L:90 later records that account as rollback operator. It identifies a nonpublic operational account rather than a generic username example. |
| `39467113293617e59a7450dc` | `TLSv1.3` | keep | **Ambiguous** | L:8 records the actual startup `tls` configuration. The exact target names a public standard, and P:9–10 describes that standard publicly, supporting keep. Its occurrence also states the particular service's operational configuration; v1 does not expressly resolve when that fact becomes an internal arrangement rather than permissible generic-standard naming. Unlike E's standards names, source L has no express external-sharing permission for this association. |
| `3a90945b357f211e8e4405c1` | `questions` | keep | Compatible | L:85 selects the generic payload field name, not its neighboring request content. P:26 and P:34 confirm its ordinary schema role; it identifies no account, resource, or commercial fact. |
| `5497da7a9319bdc664b1c2fe` | `Ethernet` | keep | Compatible | E:93–94 calls it common technology, and E:106–107 permits external standards names. It is not an internal code name or a specific resource identifier. |
| `712ac776113a54e734b68302` | `Павел Олегович Туманов` | mask | Compatible | E:4 names the contractor's engineer who prepared this restricted proposal; E:86–87 links the same person to contractor contact details. The private professional/project association is protected even if a person's name could be public elsewhere. |
| `831edde3c0cee04bd7145b33` | `ООО «Медный Ток»` | mask | Compatible | E:4 identifies the preparing engineer's company; E:86–87 confirms the contractor role. It discloses a party to the nonpublic proposal, not merely a public company-name mention. E:106–107 withholds party details. |
| `9f7cf5bb1cb317f5a0dcbb5a` | `+7 (000) 518-24-09` | mask | Compatible | E:86–87 is the particular contractor contact's phone. Restricted relationship/contact context supports mask; the synthetic-looking number format does not supply publication permission. |
| `b1763b72031134e7894a12f9` | `cache01.cedar.test` | mask | Compatible | L:5 is the actual Redis host, beside its internal address and password. It identifies a live represented internal resource rather than a reserved-domain illustration. |
| `b26096641f31a46b0a94cb1f` | `АО «Кедровые лаборатории»` | mask | Compatible | E:3 names the approving organization; E:85 identifies its representative as the customer contact. It reveals a party/customer relationship in a restricted proposal, with party details withheld at E:106–107. |
| `be9aebd166e52a29583cd807` | `Кедровый Атлас` | mask | Compatible | E:10 names the internal project whose laboratory/office site is the estimate's object. E:89 ties it to the internal commercial-delivery path; E:106–107 withholds internal project composition. |
| `d70bb17f7591d4b37545511b` | `cedar-api` | mask | Compatible | L:1 identifies the actual bootstrapped staging service and associated project; later request/rollback entries reuse it. It is an internal service identifier, not a general technology name. |

## Interpretation limits

The three ambiguous `keep` controls limit a claim that this frozen calibration is perfectly aligned with policy v1. This finding does not invalidate the 29 supported controls, establish a definite contradiction, or authorize retrospective relabeling. Any scores produced using the frozen fixture remain scores against that fixture; this audit supplies a separate policy-compatibility caveat and must not be treated as a model result or a new scoring oracle.

The calibration does not sample the estimate's ordinary quantities or composition date. E:17–18 says those are not themselves closed prices; that is narrower than unrestricted external disclosure. E:106–107 also withholds the internal project's composition. It would therefore be unsound to infer either that all quantities/dates are public or that every number in the restricted estimate is private. Those unselected corpus labels have not been certified by this 32-candidate audit.

No policy change, gold-label change, fixture change, inference run, download, commit, or push was performed for this audit.

## User adjudication before variant B

On 15 November the user answered: «Да, общие сведения оставляем открытыми».
The question explicitly covered the standard VAT rate, the public model name,
and the TLS version in working documents unless the choice itself is separately
marked confidential. Under this intended interpretation all three disputed
`keep` labels remain correct. The original table above preserves the independent
source-only assessment before this clarification; it does not describe unresolved
user requirements. Policy bytes, the 32 candidates and their frozen labels were
not changed, and no new model result was used to set these labels.
