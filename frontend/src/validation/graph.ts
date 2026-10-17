import type { GraphResponse } from "../types";
import { array, boolean, count, id, number, record, string, unique, ValidationError } from "./base";

export function parseGraph(value: unknown, path = "graph"): GraphResponse {
  const v = record(value, path);
  const nodes = unique(array(v.nodes, (item, p = "node") => {
    const n = record(item, p);
    return { doc_id: id(n.doc_id, `${p}.doc_id`), title: string(n.title, `${p}.title`),
      chunks: count(n.chunks, `${p}.chunks`), chars: count(n.chars, `${p}.chars`) };
  }, `${path}.nodes`), node => node.doc_id, `${path}.nodes`);
  const nodeIds = new Set(nodes.map(node => node.doc_id));
  const edges = array(v.edges, (item, p = "edge") => {
    const e = record(item, p);
    const source = id(e.source, `${p}.source`), target = id(e.target, `${p}.target`);
    if (!nodeIds.has(source)) throw new ValidationError(`${p}.source`, "没有对应节点");
    if (!nodeIds.has(target) || target === source) throw new ValidationError(`${p}.target`, "必须指向另一已有节点");
    return { source, target, weight: number(e.weight, `${p}.weight`, 0, 1),
      links: id(e.links, `${p}.links`), similarity: number(e.similarity, `${p}.similarity`, 0, 1) };
  }, `${path}.edges`);
  return { nodes, edges, truncated: boolean(v.truncated, `${path}.truncated`) };
}
