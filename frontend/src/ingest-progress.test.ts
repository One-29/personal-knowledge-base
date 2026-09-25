import { describe, expect, it } from "vitest";

import { ingestProgress } from "./ingest-progress";
import type { IngestTask } from "./types";

function task(overrides: Partial<IngestTask> = {}): IngestTask {
  return {
    ingest_version: 1,
    status: "running",
    stage: "embedding",
    attempt_count: 1,
    recovery_count: 0,
    last_error_code: null,
    started_at: "2026-09-25T00:00:00Z",
    finished_at: null,
    updated_at: "2026-09-25T00:00:01Z",
    ...overrides,
  };
}

describe("ingest progress", () => {
  it("maps a running stage to stable user-facing progress", () => {
    expect(ingestProgress(task())).toEqual({
      label: "生成索引",
      percent: 72,
      detail: "第 1 次尝试",
    });
  });

  it("shows restart recovery without exposing file paths", () => {
    expect(ingestProgress(task({ recovery_count: 2 }))?.detail).toBe(
      "第 1 次尝试 · 已恢复 2 次",
    );
  });

  it("hides terminal task records", () => {
    expect(ingestProgress(task({ status: "succeeded", stage: "complete" }))).toBeNull();
  });
});
