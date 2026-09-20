import { api } from "./api";
import { byId } from "./dom";
import { friendlyError } from "./format";
import { notify } from "./notifications";
import { state } from "./state";
import type { GraphEdge, GraphNode, GraphResponse } from "./types";

interface PositionedNode extends GraphNode {
  x: number;
  y: number;
  vx: number;
  vy: number;
  r: number;
}

interface PositionedEdge extends GraphEdge {
  a: PositionedNode;
  b: PositionedNode;
}

interface GraphState {
  nodes: PositionedNode[];
  edges: PositionedEdge[];
  dragging: PositionedNode | null;
  hover: number | null;
  frame: number | null;
  alpha: number;
  requestId: number;
}

const graphState: GraphState = {
  nodes: [],
  edges: [],
  dragging: null,
  hover: null,
  frame: null,
  alpha: 1,
  requestId: 0,
};

function canvasContext(): [HTMLCanvasElement, CanvasRenderingContext2D] {
  const canvas = byId<HTMLCanvasElement>("map-canvas");
  const context = canvas.getContext("2d");
  if (context === null) throw new Error("当前浏览器不支持 Canvas 2D");
  return [canvas, context];
}

function drawGraph(): void {
  const [canvas, context] = canvasContext();
  context.clearRect(0, 0, canvas.width, canvas.height);

  const style = getComputedStyle(document.documentElement);
  const rule = style.getPropertyValue("--line-strong").trim() || "#C4CFCA";
  const stamp = style.getPropertyValue("--accent").trim() || "#AD4A3C";
  const ink = style.getPropertyValue("--ink").trim() || "#182522";
  const ledger = style.getPropertyValue("--primary").trim() || "#173F3D";
  const sans = '"Segoe UI Variable", "Microsoft YaHei UI", sans-serif';

  for (const edge of graphState.edges) {
    const active = graphState.hover === edge.a.doc_id || graphState.hover === edge.b.doc_id;
    context.strokeStyle = active ? stamp : rule;
    context.globalAlpha = active ? 0.9 : 0.25 + edge.weight * 0.5;
    context.lineWidth = 0.6 + edge.weight * 3;
    context.beginPath();
    context.moveTo(edge.a.x, edge.a.y);
    context.lineTo(edge.b.x, edge.b.y);
    context.stroke();
  }
  context.globalAlpha = 1;

  for (const node of graphState.nodes) {
    const hovered = graphState.hover === node.doc_id;
    context.beginPath();
    context.arc(node.x, node.y, node.r, 0, Math.PI * 2);
    context.fillStyle = hovered ? stamp : ledger;
    context.globalAlpha = hovered ? 1 : 0.82;
    context.fill();
    context.globalAlpha = 1;

    context.font = `${hovered ? "600 " : ""}12px ${sans}`;
    context.textAlign = "center";
    const label = node.title.replace(/\.(md|txt)$/i, "").slice(0, 16);
    const labelY = node.y + node.r + 17;
    const labelWidth = context.measureText(label).width;
    context.fillStyle = "rgba(255, 255, 255, .82)";
    context.fillRect(node.x - labelWidth / 2 - 3, labelY - 11, labelWidth + 6, 15);
    context.fillStyle = ink;
    context.fillText(label, node.x, labelY);
  }
}

function tickGraph(): void {
  if (document.hidden || state.activeView !== "map") {
    graphState.frame = null;
    return;
  }
  const { nodes, edges, alpha } = graphState;

  for (let leftIndex = 0; leftIndex < nodes.length; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < nodes.length; rightIndex += 1) {
      const left = nodes[leftIndex];
      const right = nodes[rightIndex];
      if (left === undefined || right === undefined) continue;
      let deltaX = left.x - right.x;
      let deltaY = left.y - right.y;
      const distance = Math.hypot(deltaX, deltaY) || 0.01;
      const force = (6000 * alpha) / (distance * distance);
      deltaX /= distance;
      deltaY /= distance;
      left.vx += deltaX * force;
      left.vy += deltaY * force;
      right.vx -= deltaX * force;
      right.vy -= deltaY * force;
    }
  }

  for (const edge of edges) {
    let deltaX = edge.b.x - edge.a.x;
    let deltaY = edge.b.y - edge.a.y;
    const distance = Math.hypot(deltaX, deltaY) || 0.01;
    const target = 290 - edge.weight * 55;
    const force = ((distance - target) / distance) * 0.018 * alpha * (0.4 + edge.weight);
    deltaX *= force;
    deltaY *= force;
    edge.a.vx += deltaX;
    edge.a.vy += deltaY;
    edge.b.vx -= deltaX;
    edge.b.vy -= deltaY;
  }

  for (const node of nodes) {
    node.vx += (500 - node.x) * 0.0014 * alpha;
    node.vy += (300 - node.y) * 0.0014 * alpha;
    if (node === graphState.dragging) {
      node.vx = 0;
      node.vy = 0;
      continue;
    }
    node.vx *= 0.82;
    node.vy *= 0.82;
    node.x = Math.max(node.r + 42, Math.min(958 - node.r, node.x + node.vx));
    node.y = Math.max(node.r + 34, Math.min(578 - node.r, node.y + node.vy));
  }

  graphState.alpha = alpha * 0.985;
  drawGraph();
  graphState.frame = graphState.alpha > 0.022 || graphState.dragging !== null
    ? window.requestAnimationFrame(tickGraph)
    : null;
}

function prepareGraph(data: GraphResponse): void {
  const count = data.nodes.length;
  const radius = Math.min(230, 80 + count * 9);
  graphState.nodes = data.nodes.map((node, index) => {
    const angle = (index / count) * Math.PI * 2;
    return {
      ...node,
      x: 500 + Math.cos(angle) * radius,
      y: 300 + Math.sin(angle) * radius,
      vx: 0,
      vy: 0,
      r: 8 + Math.min(14, node.chunks * 1.6),
    };
  });
  const nodeById = new Map(graphState.nodes.map((node) => [node.doc_id, node]));
  graphState.edges = data.edges.flatMap((edge) => {
    const left = nodeById.get(edge.source);
    const right = nodeById.get(edge.target);
    return left === undefined || right === undefined ? [] : [{ ...edge, a: left, b: right }];
  });
  graphState.alpha = 1;
  if (graphState.frame !== null) window.cancelAnimationFrame(graphState.frame);
  tickGraph();
}

export async function loadGraph(): Promise<void> {
  const kbId = byId<HTMLSelectElement>("map-kb").value;
  const threshold = byId<HTMLSelectElement>("map-threshold").value;
  const empty = byId("map-empty");
  const status = byId("map-stat");
  const loading = byId("map-loading");
  const requestId = ++graphState.requestId;
  if (!kbId) {
    empty.textContent = "先建一个知识库并导入笔记，这里会出现笔记之间的关联。";
    empty.classList.remove("hidden");
    status.textContent = "暂无可计算的数据";
    graphState.nodes = [];
    graphState.edges = [];
    drawGraph();
    return;
  }

  loading.classList.remove("hidden");
  empty.classList.add("hidden");
  status.textContent = `正在按 ${threshold} 阈值计算…`;
  try {
    const data = await api<GraphResponse>(`/graph?kb_id=${encodeURIComponent(kbId)}&min_similarity=${encodeURIComponent(threshold)}`);
    if (requestId !== graphState.requestId) return;
    if (data.nodes.length < 2) {
      empty.textContent = "这个知识库至少需要两篇已完成处理的文档，才能展示语义关联。";
      empty.classList.remove("hidden");
      status.textContent = `${data.nodes.length} 篇文档 · 暂无关联图`;
      graphState.nodes = [];
      graphState.edges = [];
      drawGraph();
      return;
    }
    prepareGraph(data);
    status.textContent = `${data.nodes.length} 篇文档 · ${data.edges.length} 条关联 · 阈值 ${threshold}${data.truncated ? " · 已达到计算上限" : ""}`;
  } catch (error) {
    if (requestId !== graphState.requestId) return;
    graphState.nodes = [];
    graphState.edges = [];
    drawGraph();
    status.textContent = "关联图计算失败";
    empty.textContent = friendlyError(error, "计算关联图");
    empty.classList.remove("hidden");
    notify(friendlyError(error, "计算关联图"), true);
  } finally {
    if (requestId === graphState.requestId) loading.classList.add("hidden");
  }
}

function pickNode(event: MouseEvent, canvas: HTMLCanvasElement): PositionedNode | null {
  const rectangle = canvas.getBoundingClientRect();
  const x = (event.clientX - rectangle.left) * (canvas.width / rectangle.width);
  const y = (event.clientY - rectangle.top) * (canvas.height / rectangle.height);
  return graphState.nodes.find((node) => Math.hypot(node.x - x, node.y - y) <= node.r + 4) ?? null;
}

export function initGraph(): void {
  const canvas = byId<HTMLCanvasElement>("map-canvas");
  const hover = byId("map-hover");
  canvas.addEventListener("mousemove", (event) => {
    const node = pickNode(event, canvas);
    graphState.hover = node?.doc_id ?? null;
    if (node === null) {
      hover.textContent = "";
    } else {
      const links = graphState.edges
        .filter((edge) => edge.a.doc_id === node.doc_id || edge.b.doc_id === node.doc_id)
        .map((edge) => edge.a.doc_id === node.doc_id ? edge.b.title : edge.a.title);
      hover.textContent = `${node.title} · ${node.chunks} 块 · ${node.chars} 字${links.length > 0 ? ` · 关联：${links.slice(0, 4).join("、")}` : " · 暂无关联"}`;
    }
    drawGraph();
  });
  canvas.addEventListener("mousedown", (event) => {
    const node = pickNode(event, canvas);
    if (node === null) return;
    graphState.dragging = node;
    graphState.alpha = Math.max(graphState.alpha, 0.4);
    if (graphState.frame === null) tickGraph();
  });
  window.addEventListener("mouseup", () => { graphState.dragging = null; });
  canvas.addEventListener("mousemove", (event) => {
    if (graphState.dragging === null) return;
    const rectangle = canvas.getBoundingClientRect();
    graphState.dragging.x = (event.clientX - rectangle.left) * (canvas.width / rectangle.width);
    graphState.dragging.y = (event.clientY - rectangle.top) * (canvas.height / rectangle.height);
  });
  byId("map-kb").addEventListener("change", () => { void loadGraph(); });
  byId("map-threshold").addEventListener("change", () => { void loadGraph(); });
  byId("map-reload").addEventListener("click", () => { void loadGraph(); });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.activeView === "map" && graphState.nodes.length > 0 && graphState.frame === null) {
      graphState.alpha = Math.max(graphState.alpha, 0.12);
      tickGraph();
    }
  });
}
