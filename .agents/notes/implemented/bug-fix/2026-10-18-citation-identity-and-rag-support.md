# Agent Note: Source identity and bounded RAG support checks

Status: implemented

## Decision

SQLite v3 uses AUTOINCREMENT for library, document and chunk identities. The v2 upgrade rebuilds these tables inside an explicit write transaction, preserves rows and dependent indexes/triggers, verifies foreign keys, and rolls back schema and data together on failure. PostgreSQL Identity is unchanged. Previously deleted SQLite IDs cannot be reconstructed; online citation details must therefore match their saved snapshot even after a successful HTTP response.

Frontend source validation covers formal citations and candidates across workflow steps. Network conflicts fail parsing; saved conflicts are quarantined in every affected numbering scope. UI bindings use the nearest scope plus original number and chunk ID, and preview caching distinguishes different snapshots of reused IDs and different display numbers. Readable history and valid versioned image metadata remain available; missing historical image bytes cannot be recreated.

Generation receives both the original question and the resolved follow-up. Only retrieved text supplies factual evidence. Context expansion adds a bounded adjacent-unit window to original retrieval anchors, while added blocks only close explicit sentence/condition dependencies. Window context does not automatically become a formal citation: conflating the two incorrectly included unrelated sources in source-lookup steps, which the existing workflow regressions detect.

The semantic support stage is enabled by default and uses an independent request with the existing LLM configuration. Every nonempty draft line receives a checked judgment, and supported judgments require excerpts from permitted original sources. Missing judgments, invalid excerpts, network failure and context overflow prevent normal publication. The final source-version check runs after the model wait. Source lookup's rule-generated document names and offsets do not require a second semantic model call.

## Alternatives and limits

ID checks alone cannot recognize a successful response for a replaced chunk. Comparing snapshot details protects legacy histories, while monotonic allocation prevents new automatic reuse. Rejecting entire histories would discard readable text and valid sources unnecessarily.

Uniformly treating every adjacent sentence as a required citation pollutes workflow evidence. Optional windows and mandatory dependency closure are separate; windows are bounded and do not recursively consume a whole document. This remains a heuristic and cannot resolve all long-distance coreference.

The semantic stage adds latency and token cost. Its judgments can still be wrong, including correlated errors from the same model family. The coverage/excerpt validator proves response structure and source membership, not logical entailment. Disabling ANSWER_SUPPORT_CHECK_ENABLED restores structural/version-only verification; SSE drafts may be visible before the final result replaces them.

## Verification

Failure-first tests reproduce SQLite ID reuse, successful-response snapshot mismatch, cross-step candidate collisions, missing follow-up context and missed cross-sentence conditions. Migration tests verify rollback, FTS, custom indexes, foreign keys and idempotence. Semantic tests cover supported/unsupported/contradicted decisions, missing/duplicate units, fabricated excerpts, source mismatch, network/JSON failure, budget overflow, source changes during verification, explicit disablement and real synchronous/SSE HTTP paths.

Existing tests retain their assertions. Their fake support provider replaces only the model transport; production coverage, excerpt and publication checks still execute. Dedicated semantic tests replace that provider with controlled decisions and corrupt responses. These tests do not measure a real model's accuracy or run the paid v2 quality benchmark. Full regression results and GitHub checks are reported with the delivered commit.

Local verification passed: frontend type checking, 109 frontend tests and production build; 530 backend tests in an isolated Python 3.13 environment with PostgreSQL and SQLite; PostgreSQL empty-database Alembic upgrade and schema-diff check. The backend run emitted one upstream Starlette/httpx deprecation warning. No production database or saved user configuration was used for these checks.
