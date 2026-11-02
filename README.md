<!-- bankiru-reviews README — explicit <a id> anchors for TOC; section headers use · [↑](#toc). -->
<p align="center">
  <img src="assets/bankiru-logo-white.svg" alt="banki.ru logo" width="500">
</p>

<h1 align="center">bankiru-reviews</h1>

<p align="center">
  Dockerized stack that collects negative customer reviews from
  <a href="https://www.banki.ru">banki.ru</a>, stores them in Postgres,
  and serves filtered, LLM-summarized exports through a Gradio web UI
  gated by <a href="https://goauthentik.io/">Authentik</a> OIDC, with
  all secrets pulled at start-up from a self-hosted
  <a href="https://infisical.com/">Infisical</a> instance into tmpfs.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/banki.ru-reviews-00a859?style=flat-square" alt="banki-ru">
  <img src="https://img.shields.io/badge/cloud.ru-Advanced-1a73e8?style=flat-square" alt="cloud-ru">
  <img src="https://img.shields.io/badge/gradio-UI-f97316?style=flat-square&logo=gradio&logoColor=white" alt="gradio">
  <img src="https://img.shields.io/badge/fastapi-API-009688?style=flat-square&logo=fastapi&logoColor=white" alt="fastapi">
  <img src="https://img.shields.io/badge/apscheduler-cron-4051b5?style=flat-square" alt="apscheduler">
</p>

---

<a id="toc"></a>

## Table of contents

- [Architecture at a glance](#architecture-at-a-glance)
- [Quick start](#quick-start)

Longer notes:

- [Operations, API, parser, and day-2](docs/operations.md)
- [Описание проекта (на русском)](docs/overview.ru.md)
- [Collection policy](docs/collection-policy.md)
- [Public API guide](docs/bankiru-reviews-public-api.md)


<a id="architecture-at-a-glance"></a>
## Architecture at a glance · [↑](#toc)

```mermaid
flowchart TD
    user(["User browser"]) -->|"HTTPS"| nginx["Nginx on host\n(TLS termination)"]
    guest(["API client"]) -->|"HTTPS + API-Token"| nginx

    subgraph stack [Docker Compose stack: bankiru-reviews]
      direction TB
      ui["ui\nGradio + OIDC"]
      api["api\nFastAPI"]
      parser["parser\nAPScheduler"]
      ui -->|"GET /reviews\nno token"| api
      parser -->|"POST /reviews\nAPI_TOKEN"| api
    end

    nginx -->|"127.0.0.1:17060"| ui
    nginx -->|"127.0.0.1:1706\n/reviews /docs /healthz"| api
    ui <-->|"OIDC"| authentik(("Authentik"))
    api -->|"SQLAlchemy"| postgres[("Postgres\nreviews + embeddings")]
    api -->|"put_object\npresigned_url"| s3[("S3 / OBS")]
    parser -->|"sequential HTTP/1.1\nbackoff + retry"| bankiruRu[("banki.ru")]
    infisical(("Infisical")) -.->|"secrets via tmpfs\n/dev/shm/...env"| stack
```

**Request path for a UI query:**

1. Browser → Nginx (TLS) → `ui` service (`127.0.0.1:17060`).
2. Gradio calls `GET /reviews` on the `api` service (internal compose network, no `API-Token`). The Format dropdown defaults to `parquet`, and `summarize` is sent explicitly (`true` only when a real **Summary model** is selected). Clearing Format omits `outputFormat`, so the API answers inline.
3. `api` queries Postgres, optionally runs the LLM when `summarize=true`, then either returns rows inline or uploads the export to S3 and returns a pre-signed URL — JSON body `{url, comment, …}` or `{reviews, comment, …}`. An omitted date bound always resolves to the matching bound of the stored data (omitted `startDate` = earliest `datePublished` in DB, omitted `endDate` = the latest one), whatever `summarize` is; the resolved range drives the SQL filter and is echoed back in the response. An inverted effective range returns **400**, and with `summarize=true` a span longer than three calendar months returns **400** before the main select/LLM.
4. On success, UI renders the summary in the Markdown panel (empty when `<no summary>`) and may show an info toast **"Download your file"** only when an export URL is present. On API/network failure the UI raises a Gradio **error toast** with the API `detail` (not Summary text), clears the signed URL and Summary, and does not show the download info toast. "Download reviews" opens the pre-signed URL in a new tab (**browser → OBS**, no server round-trip). "Download summary" saves the Summary Markdown as a local `.md` (client-side Blob).

**Request path for an external API client:** Browser/script → Nginx (TLS) → `api` (`127.0.0.1:1706`) with `API-Token` (guest or admin). Omitting `outputFormat` returns reviews **inline** in `reviews`. Omitting `summarize` is always **`false`** (gateway, localhost, or UI). Omitting a date bound resolves it to the earliest / latest stored review date, which is also what the response echoes back — a breaking change, since those fields are no longer `null`. Unknown query params → **422**. An inverted effective range → **400** (previously an empty 200); `summarize=true` with an effective date span &gt; 3 calendar months → **400** with a fixed `detail` string (same for Nginx, localhost, and Gradio); a `keywords` query the embedding provider cannot embed → **503** (previously an empty 200). Full guide: [`docs/bankiru-reviews-public-api.md`](docs/bankiru-reviews-public-api.md).

---

<a id="quick-start"></a>
## Quick start · [↑](#toc)

<a id="production-with-infisical"></a>
### Production (with Infisical) · [↑](#toc)

```bash
# 1. Install the Infisical CLI (once per host)
curl -1sLf 'https://artifacts-cli.infisical.com/setup.deb.sh' | sudo -E bash
sudo apt-get update && sudo apt-get install -y infisical

# 2. Clone
git clone <repo-url> ~/git/bankiru-reviews
cd ~/git/bankiru-reviews

# 3. Register the OIDC client in Authentik (see "UI and authentication" in docs/operations.md);
#    populate OIDC_CLIENT_ID / OIDC_CLIENT_SECRET / GUEST_API_TOKEN in Infisical.

# 4. Start the stack (prompts for the Infisical client secret)
./scripts/start.sh

# 5. Provision / reload the Nginx vhost AFTER api is up with loopback + gateway auth
sudo cp config/bankiru-reviews.conf /etc/nginx/conf.d/bankiru.conf
sudo certbot certonly --webroot -w /var/www/html \
     -d bankiru.uva-advanced.ru -d www.bankiru.uva-advanced.ru
sudo nginx -t && sudo systemctl reload nginx

# 6. Verify
docker compose --env-file /dev/shm/bankiru-reviews-secrets/.env ps
curl http://127.0.0.1:1706/healthz   # {"status": "ok"}
curl -sS https://bankiru.uva-advanced.ru/healthz
open https://bankiru.uva-advanced.ru
```

<a id="startsh-flags"></a>
### `start.sh` flags · [↑](#toc)

```bash
./scripts/start.sh               # fetch secrets + docker compose up -d
./scripts/start.sh --no-start    # fetch secrets only (skip compose up)
./scripts/start.sh --refresh     # re-fetch secrets + force-recreate api/parser/ui
```

Pass the client secret non-interactively:

```bash
INFISICAL_CLIENT_SECRET=… ./scripts/start.sh
```

<a id="local-development-without-docker"></a>
### Local development (without Docker) · [↑](#toc)

```bash
cp .env.example .env
# Fill in at minimum:
#   POSTGRES_URL, OBS_*, API_TOKEN, OPENAI_API_KEY,
#   SESSION_MIDDLEWARE_SECRET, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET

uv sync
uv run python -m bankiru.api      # terminal 1 — http://localhost:1706
uv run python -m bankiru.parser   # terminal 2 — fires at 00:05 by default
uv run python -m bankiru.ui       # terminal 3 — http://localhost:17060
```

Trigger a one-off crawl (bypassing the cron schedule):

```bash
uv run python -c "
from bankiru.logging import configure_logfire; configure_logfire('parser')
import asyncio; from bankiru.parser.runner import run_once; asyncio.run(run_once())
"
```

Trigger a one-off crawl for a specific date range (e.g. May 17–18):

```bash
uv run python -c "
from bankiru.logging import configure_logfire; configure_logfire('parser')
import asyncio; from bankiru.parser.runner import run_once
asyncio.run(run_once(start_date='2026-05-17', end_date='2026-05-19'))
"
```

> **Note:** `end_date` is *exclusive* (the crawl window is `[start_date, end_date)`).
> To collect reviews published on May 17 and May 18, set `end_date` to May 19.

<a id="tests"></a>
### Tests · [↑](#toc)

```bash
uv sync --group dev
uv run pytest
```

The suite needs neither Postgres nor S3: the session and S3 dependencies are
replaced with fakes through `app.dependency_overrides`, and `tests/conftest.py`
supplies the required environment variables (including an empty
`OPENAI_API_KEY`, which keeps the UI import offline). The LLM and the embedder
are always stubbed, through the `summarizer`, `embedder`, `broken_embedder` and
`empty_embedder` fixtures — no test reaches either provider.

**The public gateway in tests.** From the application's point of view a request
through Nginx differs in exactly one way: the `X-Bankiru-Gateway: 1` header,
which the proxy overwrites so a client cannot forge its absence. The tests set
that header themselves (`conftest.gateway()`). Since this layer either passes a
request through untouched or answers 403, the rest of the suite runs without it
and only a representative few scenarios are repeated through the gateway. What
only a real deployment can show — TLS, the proxy setting the header, an error
body reaching the client unaltered — is checked by
[`scripts/check-public-api.sh`](scripts/check-public-api.sh) against the running
service.

#### Layer 1 — authorization · `tests/test_auth_gateway.py`, `tests/test_guest_tokens.py`

| Condition | Outcome | Test |
|-----------|---------|------|
| No gateway header, no token | 200 — the UI and anything on the compose network | `test_an_internal_caller_needs_no_token` |
| No gateway header, nonsense token | 200 — the token is not looked at | `test_an_internal_caller_may_send_any_token` |
| Gateway header set to `0`, `true`, `2`, or empty | 200 — only the exact value `1` engages the check | `test_only_the_exact_header_value_engages_the_check` |
| Gateway + guest token (the secret from either `owner:token` pair) or admin token | 200 | `test_accepted_tokens` |
| Gateway + the `owner:token` pair itself | 403 — the header is the secret only | `test_the_owner_token_pair_is_not_a_credential` |
| Gateway, no token | 403 | `test_a_missing_token_is_refused` |
| Gateway + empty, wrong, or shorter token | 403 — a length mismatch must not raise out of `compare_digest` | `test_refused_tokens` |
| Gateway, no token | Neither query runs | `test_a_refused_caller_reads_no_data` |
| Gateway, no token **and** an unknown parameter | 403 — authorization is decided first | `test_authorization_precedes_query_validation` |
| Guest token on any of the four write routes | 403 — guests never write, gateway header or not, and no statement runs | `test_a_guest_cannot_post`, `test_a_guest_cannot_delete`, `test_a_guest_cannot_delete_by_date`, `test_a_guest_cannot_delete_duplicates`, `test_no_write_route_touches_the_database_unauthorized` |
| Any write route with an absent or empty `API-Token` header | 401, not 403 — `APIKeyHeader` rejects before `api_token` runs | `test_an_absent_write_token_is_401_not_403` |
| `GET /healthz` through the gateway, no token | 200 — Docker's healthcheck depends on it | `test_healthz_needs_no_token_through_the_gateway` |
| `GET /` | Redirect to `/docs` | `test_the_root_redirects_to_the_docs` |
| `GUEST_API_TOKEN` empty | No guest tokens | `test_empty_string_yields_no_tokens` |
| Two `owner:token` pairs | Only the secrets are kept | `test_two_pairs_keep_only_the_tokens` |
| Spaces around commas and colons | Stripped | `test_whitespace_around_commas_and_colons_is_stripped` |
| Trailing comma | Ignored | `test_a_trailing_comma_is_ignored` |
| Empty comma segments (leading, middle, trailing) | Skipped | `test_empty_comma_segments_are_skipped` |
| Token containing colons | Kept in full after the first `:` | `test_a_token_may_contain_colons` |
| `list[str]` constructor argument | Kept as tokens (no pair parse) | `test_a_list_is_kept_as_tokens` |
| Bare token, missing `:`, empty owner/secret, or mixed malformed | `ValidationError` at settings load | `test_malformed_entries_are_rejected` |

#### Layer 2 — query validation · `tests/test_query_validation.py`, `tests/test_schemas.py`

| Condition | Outcome | Test |
|-----------|---------|------|
| `outputFormat` = `csv`, `json`, `parquet`, `xlsx` | 200, `filename` ends with that extension | `test_each_format_is_accepted` |
| The handler set discovered by introspection | Exactly those four | `test_every_handler_is_reachable_by_name` |
| `outputFormat` = `pdf`, `CSV`, or empty | 422 | `test_other_formats_are_rejected` |
| `summarize` = `true`/`false`/`1`/`0`/`yes`/`no`/`on`/`off` | 200, echoed as a boolean | `test_accepted_booleans` |
| `summarize` = `maybe`, `2`, `-1` | 422 | `test_rejected_booleans` |
| `summarize` omitted | `false` for every caller | `test_summarize_defaults_to_false` |
| `bankName` / `product` / `location` given once | Echoed as a one-element list | `test_a_single_value_becomes_a_one_element_list` |
| The same parameter repeated | Every value collected | `test_a_repeated_parameter_collects_every_value` |
| A date as `2026-03-01`, `20260301`, or `2026-3-1` | All three mean 1 March 2026 | `test_both_date_spellings_reach_the_same_bound` |
| A date as an empty string | Treated as omitted, then resolved | `test_an_empty_date_is_resolved_like_an_omitted_one`, `test_empty_string_means_no_bound` |
| A `datetime` passed to the model | Truncated to a date | `test_datetime_input_is_normalized_to_date` |
| `not-a-date`, `2026-13-45`, `01-03-2026`, `20260231`, `2026-03-01T12:00` | 422 | `test_malformed_dates_are_rejected`, `test_malformed_date_is_rejected` |
| An unknown parameter | 422, `extra_forbidden`, naming the parameter | `test_an_unknown_parameter_names_itself`, `test_unknown_query_param_is_rejected` |
| Several unknown parameters | All of them reported | `test_every_unknown_parameter_is_reported` |
| An unknown parameter | No query reaches the database | `test_validation_precedes_any_database_work` |
| `cloudModel` with `summarize=false` | Echoed, but nothing is summarized | `test_cloud_model_without_summarize_is_inert` |

#### Layer 3 — date bounds · `tests/test_date_resolution.py`, `tests/test_date_bounds_sql.py`, `tests/test_inverted_range.py`, `tests/test_summarize_limit.py`, `tests/test_empty_table.py`

| Condition | Outcome | Test |
|-----------|---------|------|
| `startDate` omitted | Earliest stored `datePublished`, whatever `summarize` is | `test_omitted_start_resolves_to_min` |
| `endDate` omitted | Latest stored `datePublished` — never "today" | `test_omitted_end_resolves_to_max`, `test_bounds_do_not_depend_on_the_current_date` |
| Both omitted | The full span of the stored data | `test_both_omitted_span_the_stored_data` |
| Both given | The bounds query is skipped entirely | `test_no_bounds_query_when_both_dates_given` |
| Other filters given | Bounds are still read over the whole table, so a narrow filter cannot dodge the limit | `test_bounds_query_ignores_the_other_filters` |
| A guest through the gateway | The same bounds as an internal caller | `test_a_guest_through_the_gateway_gets_the_same_bounds` |
| `min()` / `max()` return `date` rather than `datetime` | Accepted | `test_plain_date_bounds_are_accepted` |
| Any resolved range | Inclusive in SQL: `00:00:00` to `23:59:59.999999` | `test_bounds_are_inclusive`, `test_single_day_range_covers_that_day` |
| Dates omitted | The resolved bounds still reach the SQL filter | `test_resolved_bounds_are_applied_when_dates_are_omitted` |
| `keywords` or `summarize=true` | The same bounds as the plain path | `test_semantic_search_filters_on_the_resolved_bounds`, `test_summarized_query_filters_on_the_same_bounds` |
| Effective `endDate` before `startDate` | 400 with a fixed `detail`, main query skipped | `test_explicitly_inverted_dates`, `test_start_after_the_last_stored_review`, `test_end_before_the_first_stored_review`, `test_inverted_range_skips_the_main_query` |
| Inverted **and** longer than three months with `summarize` | The inversion is reported | `test_inversion_is_reported_before_the_summarize_limit` |
| `summarize=true`, effective span exactly three calendar months | 200 | `test_exactly_three_months_is_allowed` |
| `summarize=true`, one day more | 400, before the main query | `test_one_day_over_three_months_is_rejected`, `test_rejection_precedes_the_main_query` |
| Three months across a shorter month (30 Nov → 28 Feb) | `relativedelta` clamps, moving the boundary a day | `test_three_months_are_calendar_months_not_ninety_days` |
| `summarize=false`, any span | 200 — the limit guards summarization only | `test_same_range_without_summarize_is_fine` |
| `summarize=true`, no dates at all | 400 — the incident case | `test_omitted_dates_hit_the_limit` |
| `summarize=true`, only `endDate` given over a wide table | 400 — the omitted start opens the interval to the earliest review | `test_omitted_start_over_a_wide_table_is_rejected` |
| `summarize=true`, only `startDate` given, data ending within three months | 200 — the span ends at the newest review, however old that is | `test_omitted_end_within_three_months_is_allowed`, `test_omitted_end_over_stale_data_is_allowed` |
| Empty table, dates omitted | 200 "no results", both echoed dates `null`, main query skipped | `test_empty_table_returns_no_results`, `test_empty_table_skips_the_main_query` |
| Empty table, one bound given | The given bound is echoed back; the other stays `null` | `test_one_given_bound_still_resolves_against_nothing` |
| Empty table, `summarize=true`, dates omitted | 200 — resolution finds no bounds, so the span check never runs | `test_empty_table_does_not_trip_the_summarize_limit` |
| Empty table, both dates given | Bounds are not resolved; an ordinary "no results" with the dates echoed (a span &gt; 3 months with `summarize=true` is still **400**) | `test_explicit_dates_take_the_ordinary_no_results_path` |

#### Layer 4 — filters and the query path · `tests/test_filters_sql.py`

| Condition | Outcome | Test |
|-----------|---------|------|
| No filters | Only the two date comparisons in `WHERE` | `test_without_filters_only_the_dates_are_constrained` |
| `bankName` / `product` | `IN (...)`, exact match | `test_exact_filters_compile_to_in` |
| `location` | `LIKE 'value' \|\| '%'` — prefix match | `test_location_matches_by_prefix` |
| Several locations | Combined with `OR` | `test_several_locations_are_combined_with_or` |
| Dates and all three filters | All combined with `AND` | `test_all_filters_apply_together` |
| No `keywords` | Ordered, and with no `LIMIT` — a broad filter can produce a huge export | `test_the_standard_path_is_ordered_and_unlimited` |
| `keywords` empty or whitespace only | The standard path; the embedder is never called | `test_blank_keywords_take_the_standard_path` |
| `keywords` with text | `JOIN review_embeddings`, ranked by cosine distance, capped by `SEMANTIC_SEARCH_LIMIT` | `test_the_semantic_path_joins_embeddings_and_ranks_by_distance`, `test_the_semantic_limit_comes_from_settings` |
| `keywords` with text | `SET LOCAL hnsw.ef_search` is issued for the transaction | `test_the_semantic_path_raises_hnsw_recall` |
| `keywords` plus scalar filters | Ranking does not widen the filters | `test_the_semantic_path_keeps_the_scalar_filters` |
| `SEMANTIC_SEARCH_MAX_DISTANCE` set | The distance also appears as a ceiling in `WHERE` | `test_the_distance_ceiling_is_applied_when_configured` |
| `SEMANTIC_SEARCH_MAX_DISTANCE` empty | Ranking only, no ceiling | `test_no_distance_ceiling_when_disabled` |
| `SEMANTIC_SEARCH_EF_SEARCH` zero or negative | Clamped to 1 — the value is interpolated into SQL | `test_a_nonpositive_ef_search_is_clamped` |
| `bankName` / `product` given as an empty string | A real filter for the empty string: matches nothing | `test_an_empty_exact_filter_matches_nothing` |
| `location` given as an empty string | The opposite — an empty prefix matches everything | `test_an_empty_location_matches_everything` |
| `keywords` with `summarize=true` | The LLM reads the ranked, capped rows rather than the whole interval | `test_a_summary_reads_the_ranked_result_set` |

#### Layer 5 — the response · `tests/test_response_shape.py`, `tests/test_semantic_failure.py`, `tests/test_export_failures.py`

| Condition | Outcome | Test |
|-----------|---------|------|
| No `outputFormat`, rows found | `reviews` inline; `url` / `filename` `null` | `test_inline_branch` |
| An inline row | Exactly the seven documented fields, `datePublished` as `YYYY-MM-DD HH:MM:SS` | `test_inline_rows_carry_every_documented_field` |
| `outputFormat` set, rows found | `url` + `filename`, `reviews` `null`, one upload to S3 | `test_export_branch`, `test_the_export_is_uploaded_with_a_body` |
| No rows | `comment` is the "no results" message; `reviews`, `url`, `filename` all `null` | `test_no_results_branch` |
| No rows with `outputFormat` | No link to a file that was never written | `test_no_results_with_an_output_format_has_no_url` |
| No rows with `summarize=true` | The summarizer is not called | `test_no_results_never_calls_the_summarizer` |
| `summarize=true` | `comment` opens with `**Summary model:**` and the model used | `test_the_summary_names_the_default_model`, `test_an_explicit_model_overrides_the_default` |
| Duplicate review bodies | Deduplicated before the LLM sees them | `test_identical_bodies_are_summarized_once` |
| `summarize=true` with `outputFormat` | Both the summary and the download URL | `test_a_summary_and_an_export_arrive_together` |
| `summarize` false or omitted | `comment` is `null` | `test_no_summary_without_the_flag` |
| Explicit dates | Echoed unchanged, whatever `summarize` is | `test_echo_repeats_explicit_dates_unchanged` |
| Any filters | All echoed, so the response is self-describing | `test_every_filter_is_echoed_back` |
| Any response | Exactly the thirteen documented fields | `test_the_response_holds_no_unexpected_fields` |
| `keywords` the embedder cannot embed | 503 with a fixed `detail`; no rows are read | `test_embedder_failure_is_a_503`, `test_no_reviews_are_read_when_the_search_cannot_run` |
| The provider answers successfully but with no vector | The same 503, not a 500 | `test_an_empty_provider_answer_is_the_same_503` |
| The same, through the gateway | The same 503 — not to be read as an authorization failure | `test_the_same_503_arrives_through_the_gateway` |
| The same | The provider's own message (naming an internal endpoint) is logged and kept out of the body | `test_the_provider_message_stays_out_of_the_response`, `test_the_provider_message_is_logged` |
| `keywords` with a working embedder | 200; the query is embedded stripped, in `query` mode | `test_a_working_embedder_still_searches`, `test_the_query_is_stripped_before_embedding` |
| S3 refuses the upload or the pre-signed URL | 500 — never a 2xx, and no filename to retry | `test_a_failed_export_is_never_a_success`, `test_a_failed_export_names_no_file` |
| The same, with `summarize=true` | The summary is lost with the response rather than delivered alone | `test_a_lost_summary_is_not_delivered_alone` |
| No `outputFormat` with S3 unavailable | 200 — the inline path never touches the bucket | `test_an_inline_query_never_touches_s3` |
| Any export | The link keeps botocore's default lifetime, about an hour | `test_the_download_link_keeps_the_default_lifetime` |

#### Layer 6 — the Gradio UI · `tests/test_ui_params.py`, `tests/test_ui_errors.py`

| Condition | Outcome | Test |
|-----------|---------|------|
| Model dropdown at `<no summary>`, empty, or cleared | `summarize=false`, no `cloudModel` | `test_no_model_means_no_summarization` |
| A model chosen | `summarize=true` plus `cloudModel` | `test_a_chosen_model_requests_a_summary` |
| No summary wanted | `summarize=false` is still sent — the filter drops by value, not by truthiness | `test_summarize_false_is_sent_rather_than_dropped` |
| Empty dates, lists, or keywords | Omitted from the request rather than sent empty | `test_empty_inputs_are_not_sent` |
| Every input filled | All of them reach the API, lists as repeated parameters | `test_every_filled_input_reaches_the_api` |
| A format chosen in the dropdown | Passed through unchanged | `test_each_format_is_passed_through` |
| The Format dropdown cleared | No `outputFormat` is sent, so the answer comes back inline | `test_a_cleared_format_asks_for_an_inline_answer` |
| Any Submit | Nothing beyond the known parameters, which would come back as a 422 | `test_no_unexpected_parameters_are_sent` |
| API answers 400, 401, 403, or 503 | `gr.Error` toast carrying the API's `detail` verbatim; no info toast | `test_an_api_error_becomes_an_error_toast`, `test_a_failed_request_shows_no_download_prompt` |
| API answers 422 | The `loc`/`msg` list is flattened into one line | `test_validation_errors_are_flattened` |
| A non-JSON or empty error body | Falls back to the text, then to the status code | `test_non_json_body_falls_back_to_text`, `test_empty_body_falls_back_to_the_status_code` |
| A JSON `detail` string | Returned verbatim | `test_string_detail_is_returned_verbatim` |
| The API unreachable | `gr.Error` naming the network failure | `test_network_failure_becomes_an_error_toast` |
| 200 with an export URL | The "Download your file" info toast | `test_download_toast_only_fires_with_a_url` |
| 200 without a URL | No info toast — there is nothing to download | `test_no_toast_without_a_url` |
| Any failed Submit | The `.failure` listener clears the stale URL and Summary | `test_failure_handler_clears_url_and_summary` |

#### Layer 7 — parser, robots, and budgets · `tests/test_listing_parse.py`, `tests/test_robots.py`, `tests/test_client_headers.py`, `tests/test_client_deadline.py`, `tests/test_trusted_hosts.py`, `tests/test_summarizer_budget.py`

| Condition | Outcome | Test |
|-----------|---------|------|
| Listing snippet with a quote and a newline | Bodies and detail paths round-trip | `test_quote_and_newline_round_trip` |
| A third review with `ratingValue` 5 | Dropped after the count check | `test_rating_above_two_is_dropped` |
| Challenge markup | `ChallengePageError` | `test_challenge_raises` |
| No `Review` objects | `listing_records` returns `[]` | `test_zero_reviews_returns_empty` |
| Review count differs from detail anchors | `ListingParseError` | `test_unequal_counts_raise` |
| Every review is on or after `end_date` | `hit_left` is false and nothing is kept | `test_page_newer_than_window_continues` |
| The next review is before `start_date` | That review is not stored and `hit_left` is true | `test_page_stops_at_older_review` |
| Smoke diff | A new collected row is not missing; a stored row absent from the crawl is | `test_smoke_missing_stored_ignores_new_rows` |
| Listing URL without `rate[]`, and the detail URL | Allowed for `USER_AGENT` | `test_listing_and_detail_allowed` |
| A `rate[]` query | Denied by `*rate[]=*` | `test_rate_query_is_denied_and_plain_listing_is_allowed` |
| `?f[` or a comments URL | Denied | `test_filter_query_and_comments_denied` |
| `User-agent: bankiru-reviews` and `/private` | The longer prefix wins | `test_specific_agent_beats_star` |
| `Comparserbot` with `Disallow: /` | Does not apply to `USER_AGENT` | `test_comparserbot_rule_does_not_apply` |
| Identity headers | `USER_AGENT` and `ACCEPT_LANGUAGE` | `test_identity_headers` |
| A disallowed URL | No HTTP call | `test_disallow_does_not_call_transport` |
| Connect errors inside a 1 second budget | The listing stops; the detail returns `None` | `test_listing_connect_errors_stop_inside_a_short_budget`, `test_detail_connect_budget_returns_none` |
| Detail response missing | `location` is empty and the review stays | `test_missing_detail_keeps_empty_location` |
| `TRUSTED_HOSTS=172.16.89.1` | One host | `test_trusted_hosts_parses_gateway` |
| Output cap 8192 against a 200000 context | Per-call output is 8192 | `test_output_budget_is_8192` |

#### Documentation

`tests/test_documented_messages.py` keeps the prose honest: every `detail`
string quoted in `docs/operations.md` and in `docs/bankiru-reviews-public-api.md` must
match the constant in the code, and every test named in the tables above must
exist in `tests/`.

---
