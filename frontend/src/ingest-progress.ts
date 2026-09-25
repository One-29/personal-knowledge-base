import type { IngestTask, IngestTaskStage } from "./types";

interface StagePresentation {
  label: string;
  percent: number;
}

export interface IngestProgress {
  label: string;
  percent: number;
  detail: string;
}

const STAGES: Record<IngestTaskStage, StagePresentation> = {
  queued: { label: "等待执行", percent: 5 },
  validating: { label: "校验配置", percent: 15 },
  reading: { label: "读取原文", percent: 30 },
  chunking: { label: "切分内容", percent: 50 },
  embedding: { label: "生成索引", percent: 72 },
  publishing: { label: "发布索引", percent: 92 },
  complete: { label: "处理完成", percent: 100 },
};

export function ingestProgress(task: IngestTask | null): IngestProgress | null {
  if (task === null || (task.status !== "queued" && task.status !== "running")) return null;
  const stage = STAGES[task.stage];
  const attempt = task.attempt_count > 0 ? `第 ${task.attempt_count} 次尝试` : "等待后台执行";
  const recovery = task.recovery_count > 0 ? ` · 已恢复 ${task.recovery_count} 次` : "";
  return {
    label: stage.label,
    percent: stage.percent,
    detail: `${attempt}${recovery}`,
  };
}
