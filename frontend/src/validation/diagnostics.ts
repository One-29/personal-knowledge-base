import type { DiagnosticsResponse } from "../types";
import { nullable, record, string } from "./base";

export function parseDiagnostics(value: unknown, path = "diagnostics"): DiagnosticsResponse {
  const v = record(value, path);
  return { request_id: string(v.request_id, `${path}.request_id`), report: string(v.report, `${path}.report`),
    log_path: nullable(v.log_path, string, `${path}.log_path`), privacy_notice: string(v.privacy_notice, `${path}.privacy_notice`) };
}
