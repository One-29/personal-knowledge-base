import type { DocumentContent, IngestTask, KnowledgeBase, KnowledgeDocument, UploadResult } from "../types";
import { array, boolean, count, enumeration, id, nullable, record, string, timestamp, unique } from "./base";
import { parseImages } from "./evidence";

export function parseKnowledgeBase(value: unknown, path = "knowledge_base"): KnowledgeBase {
  const v = record(value, path);
  return { id: id(v.id, `${path}.id`), name: string(v.name, `${path}.name`), description: nullable(v.description, string, `${path}.description`),
    doc_count: count(v.doc_count, `${path}.doc_count`), created_at: timestamp(v.created_at, `${path}.created_at`) };
}

export function parseKnowledgeBases(value: unknown): KnowledgeBase[] {
  return unique(array(value, parseKnowledgeBase, "knowledge_bases"), kb => kb.id, "knowledge_bases");
}

function parseTask(value: unknown, path = "task"): IngestTask {
  const v = record(value, path);
  return { ingest_version: id(v.ingest_version, `${path}.ingest_version`),
    status: enumeration(v.status, ["queued", "running", "succeeded", "failed", "superseded"], `${path}.status`),
    stage: enumeration(v.stage, ["queued", "validating", "reading", "chunking", "embedding", "publishing", "complete"], `${path}.stage`),
    attempt_count: count(v.attempt_count, `${path}.attempt_count`), recovery_count: count(v.recovery_count, `${path}.recovery_count`),
    last_error_code: nullable(v.last_error_code, string, `${path}.last_error_code`),
    started_at: nullable(v.started_at, timestamp, `${path}.started_at`), finished_at: nullable(v.finished_at, timestamp, `${path}.finished_at`),
    updated_at: timestamp(v.updated_at, `${path}.updated_at`) };
}

export function parseDocument(value: unknown, path = "document"): KnowledgeDocument {
  const v = record(value, path);
  return { id: id(v.id, `${path}.id`), kb_id: id(v.kb_id, `${path}.kb_id`), title: string(v.title, `${path}.title`),
    status: enumeration(v.status, ["pending", "processing", "ready", "failed"], `${path}.status`),
    char_count: count(v.char_count, `${path}.char_count`), chunk_count: count(v.chunk_count, `${path}.chunk_count`), image_count: count(v.image_count, `${path}.image_count`),
    task: nullable(v.task, parseTask, `${path}.task`), last_error_code: nullable(v.last_error_code, string, `${path}.last_error_code`),
    last_error_message: nullable(v.last_error_message, string, `${path}.last_error_message`), processed_at: nullable(v.processed_at, timestamp, `${path}.processed_at`),
    created_at: timestamp(v.created_at, `${path}.created_at`), updated_at: timestamp(v.updated_at, `${path}.updated_at`) };
}

export function parseDocuments(value: unknown): KnowledgeDocument[] {
  return unique(array(value, parseDocument, "documents"), document => document.id, "documents");
}

export function parseUploadResult(value: unknown, path = "upload"): UploadResult {
  const v = record(value, path);
  return { document: parseDocument(v.document, `${path}.document`), content_changed: boolean(v.content_changed, `${path}.content_changed`) };
}

export function parseDocumentContent(value: unknown, path = "document_content"): DocumentContent {
  const v = record(value, path);
  const content = string(v.content, `${path}.content`);
  return { title: string(v.title, `${path}.title`), content, images: parseImages(v.images, `${path}.images`, 0, Array.from(content).length) };
}
