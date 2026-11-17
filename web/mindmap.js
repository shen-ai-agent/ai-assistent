// Sơ đồ tư duy (tab Học bài): nút là HTML (chữ xuống dòng, đo được kích thước), đường nối là SVG. Không thư viện ngoài.
// Cây từ server/study/mindmap.py: {label, note?, warn?, refs?: [doc id], children?}.
// Gốc bên trái, nhánh mọc sang phải; mỗi nhánh chính một màu. Kéo nền để di chuyển, cuộn / chụm hai ngón để phóng to.

const PALETTE = ["#a78bfa", "#60a5fa", "#34d399", "#fbbf24", "#f472b6", "#22d3ee", "#fb923c", "#a3e635", "#f87171"];
const HGAP = 40;          // khoảng ngang giữa nút cha và nút con
const PAD = 32;
const ZOOM = [0.2, 2.5];
const FIRST_VIEW = 32;    // lúc mở: mở sâu nhất mà số nút hiện không quá chừng này (cây lớn chỉ hiện nhánh chính)
const DRAG_PX = 5;        // kéo quá chừng này mới tính là kéo (không phải bấm)

const SVG_NS = "http://www.w3.org/2000/svg";
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export class MindMap {
  // opts: onSelect(node, path), refLabel(docId) → chữ trên chip bài, onRef(docId)
  constructor(box, opts = {}) {
    this.box = box;
    this.opts = opts;
    this.stage = document.createElement("div");
    this.stage.className = "mm-stage";
    this.svg = document.createElementNS(SVG_NS, "svg");
    this.svg.classList.add("mm-links");
    this.stage.append(this.svg);
    box.replaceChildren(this.stage);
    this.view = { x: 0, y: 0, k: 1 };
    this.notes = true;
    this.root = null;
    this.selected = null;
    this.size = { w: 0, h: 0 };
    this.#bindGestures();
  }

  set(tree) {
    let count = 0;
    const walk = (t, depth, parent, color) => {
      const node = { label: t.label, note: t.note ?? "", warn: !!t.warn, refs: t.refs ?? [], depth, parent, color,
        open: true, children: [] };
      count++;
      node.children = (t.children ?? []).map((c, i) => walk(c, depth + 1, node, depth === 0 ? PALETTE[i % PALETTE.length] : color));
      return node;
    };
    this.root = walk(tree, 0, null, "#e5e7eb");
    this.count = count;
    this.selected = null;
    this.#openTo(this.#firstDepth());
    this.render();
    this.fit();
  }

  // ---------- Mở / thu nhánh ----------

  #each(fn, node = this.root) {
    fn(node);
    for (const c of node.children) this.#each(fn, c);
  }

  // Tầng sâu nhất được hiện mà số nút hiện ≤ FIRST_VIEW (ít nhất là gốc + nhánh chính); #openTo(d) hiện tới tầng d
  #firstDepth() {
    const perDepth = [];
    this.#each((n) => { perDepth[n.depth] = (perDepth[n.depth] ?? 0) + 1; });
    let shown = perDepth[0] + (perDepth[1] ?? 0), depth = 1;
    while (depth + 1 < perDepth.length && shown + perDepth[depth + 1] <= FIRST_VIEW) shown += perDepth[++depth];
    return depth;
  }

  #openTo(depth) {
    this.#each((n) => { n.open = n.depth < depth; });
  }

  toggle(node) {
    if (!node.children.length) return;
    node.open = !node.open;
    this.render(node);
  }

  expandAll() {
    if (!this.root) return;
    this.#each((n) => { n.open = true; });
    this.render();
    this.fit();
  }

  // Chỉ còn tới tầng depth (1 = gốc + nhánh chính): để ôn, tự nhớ rồi mở dần
  collapseTo(depth) {
    if (!this.root) return;
    this.#openTo(depth);
    this.render();
    this.fit();
  }

  setNotes(on) {
    this.notes = on;
    if (this.root) this.render(this.selected ?? this.root);
  }

  path(node) {
    const out = [];
    for (let n = node; n; n = n.parent) out.unshift(n.label);
    return out;
  }

  select(node) {
    this.selected?.el?.classList.remove("sel");
    this.selected = node;
    node?.el?.classList.add("sel");
    this.opts.onSelect?.(node, node ? this.path(node) : []);
  }

  // ---------- Vẽ ----------

  // anchor: nút giữ nguyên chỗ trên màn hình sau khi vẽ lại (nút vừa bấm mở/thu)
  render(anchor = null) {
    const before = anchor?.el ? this.#screenOf(anchor) : null;
    for (const e of this.stage.querySelectorAll(".mm-node")) e.remove();
    const shown = [];
    const make = (node) => {
      node.el = this.#nodeEl(node);
      this.stage.append(node.el);
      shown.push(node);
      if (node.open) node.children.forEach(make);
    };
    make(this.root);
    for (const n of shown) {
      n.w = n.el.offsetWidth;
      n.h = n.el.offsetHeight;
    }
    const kids = (n) => (n.open ? n.children : []);
    const gap = (n) => (n.depth === 0 ? 18 : n.depth === 1 ? 10 : 6);
    const measure = (n) => {
      const ks = kids(n);
      const sum = ks.reduce((s, c) => s + measure(c), 0) + gap(n) * Math.max(0, ks.length - 1);
      return (n.span = Math.max(n.h, sum));
    };
    const place = (n, x, top) => {
      n.x = x;
      n.y = top + (n.span - n.h) / 2;
      const ks = kids(n);
      const sum = ks.reduce((s, c) => s + c.span, 0) + gap(n) * Math.max(0, ks.length - 1);
      let y = top + (n.span - sum) / 2;
      for (const c of ks) {
        place(c, x + n.w + HGAP, y);
        y += c.span + gap(n);
      }
    };
    measure(this.root);
    place(this.root, PAD, PAD);
    let w = 0;
    for (const n of shown) {
      n.el.style.transform = `translate(${n.x}px, ${n.y}px)`;
      w = Math.max(w, n.x + n.w);
    }
    this.size = { w: w + PAD, h: this.root.span + 2 * PAD };
    this.svg.setAttribute("width", this.size.w);
    this.svg.setAttribute("height", this.size.h);
    this.svg.replaceChildren(...shown.filter((n) => n.parent).map((n) => {
      const p = n.parent;
      const x1 = p.x + p.w, y1 = p.y + p.h / 2, x2 = n.x, y2 = n.y + n.h / 2, dx = HGAP / 2;
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`);
      path.setAttribute("stroke", n.color);
      path.setAttribute("stroke-width", n.depth === 1 ? 2.5 : 1.5);
      return path;
    }));
    if (before) {
      const after = this.#screenOf(anchor);
      this.view.x += before.x - after.x;
      this.view.y += before.y - after.y;
    }
    this.#apply();
  }

  #nodeEl(node) {
    const e = document.createElement("div");
    e.className = `mm-node d${Math.min(node.depth, 3)}${node.warn ? " warn" : ""}${node === this.selected ? " sel" : ""}${node.children.length && !node.open ? " closed" : ""}`;
    e.style.setProperty("--c", node.color);
    const label = document.createElement("span");
    label.className = "mm-label";
    label.textContent = node.label;
    e.append(label);
    if (node.refs.length && this.opts.refLabel) {
      const refs = document.createElement("span");
      refs.className = "mm-refs";
      for (const id of node.refs) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "mm-ref";
        b.textContent = this.opts.refLabel(id);
        b.title = "Mở sơ đồ bài này";
        b.onclick = (ev) => {
          ev.stopPropagation();
          if (!this.dragged) this.opts.onRef?.(id);
        };
        refs.append(b);
      }
      e.append(refs);
    }
    if (this.notes && node.note) {
      const note = document.createElement("span");
      note.className = "mm-note";
      note.textContent = node.note;
      e.append(note);
    }
    if (node.children.length) {
      const t = document.createElement("button");
      t.type = "button";
      t.className = "mm-toggle";
      let hidden = 0;
      if (!node.open) this.#each(() => hidden++, node);
      t.textContent = node.open ? "−" : `+${hidden - 1}`;
      t.title = node.open ? "Thu nhánh" : "Mở nhánh";
      t.onclick = (ev) => {
        ev.stopPropagation();
        if (!this.dragged) this.toggle(node);
      };
      e.append(t);
    }
    e.onclick = () => {
      if (!this.dragged) this.select(node);
    };
    return e;
  }

  #screenOf(node) {
    return { x: this.view.x + node.x * this.view.k, y: this.view.y + node.y * this.view.k };
  }

  #apply() {
    const { x, y, k } = this.view;
    this.stage.style.transform = `translate(${x}px, ${y}px) scale(${k})`;
  }

  // ---------- Di chuyển, phóng to ----------

  fit() {
    if (!this.root) return;
    const cw = this.box.clientWidth, ch = this.box.clientHeight;
    if (!cw || !ch) return;
    const k = clamp(Math.min(cw / this.size.w, ch / this.size.h), 0.45, 1);
    const w = this.size.w * k, h = this.size.h * k;
    const rootMid = (this.root.y + this.root.h / 2) * k;
    this.view = { k, x: w < cw ? (cw - w) / 2 : 0, y: h < ch ? (ch - h) / 2 : clamp(ch / 2 - rootMid, ch - h, 0) };
    this.#apply();
  }

  zoom(f, cx = this.box.clientWidth / 2, cy = this.box.clientHeight / 2) {
    const k = clamp(this.view.k * f, ...ZOOM);
    f = k / this.view.k;
    this.view.x = cx - (cx - this.view.x) * f;
    this.view.y = cy - (cy - this.view.y) * f;
    this.view.k = k;
    this.#apply();
  }

  #bindGestures() {
    const box = this.box;
    const pts = new Map();   // pointerId → {x, y}
    let start = null;        // chỗ bấm đầu tiên, để biết đã kéo chưa
    let pinch = null;        // khoảng cách hai ngón lần trước
    const local = (e) => {
      const r = box.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };
    box.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      pts.set(e.pointerId, local(e));
      if (pts.size === 1) {
        start = local(e);
        this.dragged = false;
      }
      pinch = null;
    });
    box.addEventListener("pointermove", (e) => {
      const prev = pts.get(e.pointerId);
      if (!prev) return;
      const cur = local(e);
      if (!this.dragged && Math.hypot(cur.x - start.x, cur.y - start.y) < DRAG_PX && pts.size === 1) return;
      if (!this.dragged) {
        this.dragged = true;   // từ đây là kéo: bấm lúc thả không mở nút
        box.setPointerCapture(e.pointerId);
        box.classList.add("dragging");
      }
      if (pts.size === 2) {
        pts.set(e.pointerId, cur);
        const [a, b] = [...pts.values()];
        const dist = Math.hypot(a.x - b.x, a.y - b.y);
        const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
        if (pinch) {
          this.view.x += mid.x - pinch.mid.x;
          this.view.y += mid.y - pinch.mid.y;
          this.zoom(dist / pinch.dist, mid.x, mid.y);
        }
        pinch = { dist, mid };
        return;
      }
      this.view.x += cur.x - prev.x;
      this.view.y += cur.y - prev.y;
      pts.set(e.pointerId, cur);
      this.#apply();
    });
    const up = (e) => {
      pts.delete(e.pointerId);
      pinch = null;
      if (!pts.size) {
        box.classList.remove("dragging");
        // click tới sau pointerup: bỏ cờ kéo sau click đó
        setTimeout(() => { this.dragged = false; }, 0);
      }
    };
    box.addEventListener("pointerup", up);
    box.addEventListener("pointercancel", up);
    box.addEventListener("wheel", (e) => {
      e.preventDefault();
      const p = local(e);
      this.zoom(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)), p.x, p.y);
    }, { passive: false });
  }
}
