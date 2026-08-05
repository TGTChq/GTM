# ATS provider fixtures — provenance (Phase 1B-2C)

These payloads drive the real `ats_board_registry.fetch_board_jobs` provider
branches offline. **None is a recorded live provider response.** They are
minimal, realistic payloads whose *shape* is taken directly from the adapter
contracts in `fetch_board_jobs` (and, where noted, from shapes already present
in the repository test-suite). No live provider request was made to create them.

Fixture source categories (per the Phase 1B-2C brief):

- **cat-2** — sanitized shape already present in repository tests.
- **cat-3** — minimal realistic payload constructed from the adapter contract.

Anonymization: all company/tenant identifiers are the placeholder `acme`; job
ids are synthetic (`101`, `L1`, `SR1`, `REQ-0`, `CS-0`, …); descriptions are
one-line placeholders. No real company, person, salary, email, token, or URL to
a real tenant is present. `93.184.216.34` (used by the harness for DNS) is the
IANA `example.com` documentation address, not a provider host.

SHA-256 is over LF-normalized bytes (stable across Windows/Unix checkout).

| SHA-256 (LF-normalized) | bytes | path | role | cat |
|---|---:|---|---|---|
| `2641e7db900ee702e7d35755c6e3c7f49237dbf069c5f1348e9f550408f5e92e` | 625 | ashby/jobs.json | listing (posting-api/job-board) | cat-3 |
| `e6538cead824cffa95f530d0d157267010269509331211902dd3a08fa00ccc25` | 6884 | cornerstone_ondemand/search_p1.json | listing page 1/2 (25 rows) | cat-3 |
| `948b83b5935cda3d1922cf500eb38ce50da2590547a64a25701f433f5f4e4274` | 1387 | cornerstone_ondemand/search_p2.json | listing page 2/2 | cat-3 |
| `59fca98466323ca7ea004659d0f8f3d0eb5ef8a325647d95413dc5024f44717e` | 582 | cornerstone_ondemand/search_small.json | listing (single page) | cat-3 |
| `9a7f8e9127aada3d86cf8714c6d0414e751d016acbf95ad9c6f215e56087d304` | 33 | greenhouse/board.json | listing (board metadata) | cat-3 |
| `9c67ad411ec7fdc92993969c82f92333bdfd90095f880ef87c7a1b2eceaf993f` | 98 | greenhouse/detail_101.json | detail (per-posting) | cat-3 |
| `4070089cdabbbd8f61ba7a7f00667fd5f6eb4f4027576e28f9ef054582786362` | 598 | greenhouse/jobs.json | listing (jobs?content=true) | cat-3 |
| `9c04cfc45e2b8a1008cb8549e5321d472c2b86db276a2f101680ad6ad9512d30` | 333 | greenhouse/jobs_malformed.json | listing (malformed-mixed) | cat-3 |
| `5165bd80a5072446d43c5211c2a30e4dc387bef9cc867eb3f082f9258b1fadaf` | 742 | lever/postings.json | listing (v0/postings) | cat-3 |
| `60521c04d6a13a0864d78615da509f2686303c71369586b37516567fc794b194` | 844 | personio/positions.xml | listing (XML feed) | cat-3 |
| `97f90262de51551c84418d16effbb4336eb7ed6f5478e7e52cc4a64694fc3973` | 546 | recruitee/offers.json | listing (api/offers) | cat-3 |
| `89dd95fae43000c0cf01bde1f76a6f9b64e101a04ecd320e3d2f1d6005bc4c1f` | 665 | smartrecruiters/detail_SRD1.json | detail (per-posting) | cat-3 |
| `7c9cf988398055f967879a73572415325b66e3b833164c243241d75abad6d867` | 187 | smartrecruiters/postings_detail.json | listing (detail scenario) | cat-3 |
| `2b39a5d4fd358d9f2d509aa67c8720f1abb9f72e1a82227df645057e2cb480f0` | 14833 | smartrecruiters/postings_p1.json | listing page 1/2 (100 rows) | cat-3 |
| `0f6dae62dee8e39cd5bbc2a0ebbc799e8e71c5ead8aae5e6ae214cccd0c6b0a5` | 788 | smartrecruiters/postings_p2.json | listing page 2/2 | cat-3 |
| `8bba1aef07c37bfdc87a2ced8d70e0b7828cc9cf57f5edbc5885f5e74bfbc6c5` | 335 | smartrecruiters/postings_small.json | listing (single page) | cat-3 |
| `aad9133988db1978d5e1353f4bc19bfeae03ee3eb51553ae97222e884be18932` | 445 | workable/account.json | listing (api/accounts?details=true) | cat-3 |
| `ebb48249dcd9149d81ac86ccc66930dddd1e835fd1c842bbc9c45bc865bc0839` | 290 | workday/detail_req-0.json | detail (cxs/job) | cat-3 |
| `da4c8e73ef49b8e348789073d3f71950a487a6c166165c85e4d4f2a8c0b4c669` | 3828 | workday/jobs_p1.json | listing page 1/2 (POST) | **cat-2** |
| `8ea861babef47fbb7ed389b2031b47c7873b2e87c817eaae4abf67f4ec3d52ad` | 991 | workday/jobs_p2.json | listing page 2/2 (POST) | **cat-2** |
| `39750c2d9ecb42659395bd75d34e52f6be080f8371c3cc95615efd3f1ab59023` | 423 | workday/jobs_small.json | listing (single page, POST) | **cat-2** |

## Per-provider schema and limitations

- **greenhouse** — `GET /v1/boards/{id}` (`{name}`) + `GET /v1/boards/{id}/jobs?content=true` (`{jobs:[{id,title,content,absolute_url,updated_at,location:{name}}]}`) + `GET /v1/boards/{id}/jobs/{id}` (`{first_published,application_deadline}`). *Limitation:* `content` HTML entities are minimal; the real feed is far larger.
- **lever** — `GET /v0/postings/{id}?mode=json&limit=N` → list of `{id,text,categories:{location,commitment},descriptionPlain,hostedUrl,createdAt,workplaceType,lists:[{content}]}`. *Limitation:* no live `applyUrl` variants exercised.
- **ashby** — `GET /posting-api/job-board/{id}` → `{jobs:[{id,title,descriptionPlain,jobUrl,location,employmentType,publishedAt,workplaceType,isRemote,isListed,secondaryLocations}]}`. One `isListed:false` row proves filtering.
- **recruitee** — `GET https://{id}.recruitee.com/api/offers/` → `{offers:[{id,title,description,careers_url,slug,location,employment_type,published_at,status,remote}]}`. One `status:closed` row proves filtering.
- **workable** — `GET https://www.workable.com/api/accounts/{id}?details=true` → `{name,jobs:[{shortcode,title,description,url,city,state,country,employment_type,published_on,telecommuting}]}`.
- **personio** — `GET {api_base}/xml?language=en` (XML `<workzag-jobs><position>…`). *Limitation:* namespace-free; the real feed may carry a namespace (the parser already strips namespaces, so this is covered).
- **smartrecruiters** — `GET {base}/companies/{id}/postings?limit=100&offset=N&destination=PUBLIC` (`{content:[…],totalFound}`) + `GET …/postings/{id}` detail (`{active,company,location:{remote},typeOfEmployment:{label},jobAd:{sections}}`). Page 1 has exactly 100 rows to force offset paging.
- **workday** — `POST {base}/wday/cxs/{tenant}/{site}/jobs` (`{jobPostings:[…],total}`) + `GET …/job/{path}` detail (`{jobPostingInfo:{…}}`). Paging shape matches `tests/test_structural_patch_ats_feed_pagination.py` (**cat-2**).
- **cornerstone_ondemand** — `GET {base}/ux/ats/careersite/{site}/api/search?page=N&pageSize=25` → `{requisitions:[…]}`. **UNVERIFIED-OFFLINE:** the live Cornerstone response shape is not confirmed against a real tenant (see the note in `fetch_board_jobs`). These fixtures validate only the *currently implemented* contract; a differing live shape would return no rows + a clean error, never fabricated jobs.
