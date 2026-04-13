// ============================================================
// brainctl — landing page
// Neural-network background + CA copy button
// ============================================================

// ---------- CA copy button ----------
(() => {
  const btn = document.getElementById("ca-copy");
  const val = document.getElementById("ca-value");
  if (!btn || !val) return;

  btn.addEventListener("click", async () => {
    const text = val.textContent.trim();
    if (!text || text.startsWith("TBA")) {
      btn.textContent = "SOON";
      setTimeout(() => (btn.textContent = "COPY"), 1400);
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      btn.textContent = "COPIED";
      setTimeout(() => (btn.textContent = "COPY"), 1400);
    } catch {
      btn.textContent = "FAIL";
      setTimeout(() => (btn.textContent = "COPY"), 1400);
    }
  });
})();

// ---------- Neural-network canvas ----------
(() => {
  const canvas = document.getElementById("neural-bg");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  let width = 0;
  let height = 0;
  let dpr = Math.min(window.devicePixelRatio || 1, 2);
  let nodes = [];
  let mouse = { x: -9999, y: -9999, active: false };

  const CONFIG = {
    density: 0.00009,       // nodes per px²
    maxLinkDist: 150,
    nodeSpeed: 0.18,
    mouseRadius: 180,
    lineColor: [180, 107, 255],
    nodeColor: [93, 242, 255],
    accentColor: [255, 79, 216],
  };

  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    width = window.innerWidth;
    height = window.innerHeight;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    seed();
  }

  function seed() {
    const count = Math.min(120, Math.max(35, Math.floor(width * height * CONFIG.density)));
    nodes = new Array(count).fill(0).map(() => ({
      x: Math.random() * width,
      y: Math.random() * height,
      vx: (Math.random() - 0.5) * CONFIG.nodeSpeed,
      vy: (Math.random() - 0.5) * CONFIG.nodeSpeed,
      r: Math.random() * 1.6 + 0.6,
      pulse: Math.random() * Math.PI * 2,
    }));
  }

  function step() {
    ctx.clearRect(0, 0, width, height);

    // Update nodes
    for (const n of nodes) {
      n.x += n.vx;
      n.y += n.vy;
      n.pulse += 0.02;
      if (n.x < 0 || n.x > width) n.vx *= -1;
      if (n.y < 0 || n.y > height) n.vy *= -1;

      // Mouse repulsion
      if (mouse.active) {
        const dx = n.x - mouse.x;
        const dy = n.y - mouse.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < CONFIG.mouseRadius * CONFIG.mouseRadius && d2 > 1) {
          const d = Math.sqrt(d2);
          const force = (1 - d / CONFIG.mouseRadius) * 0.9;
          n.x += (dx / d) * force;
          n.y += (dy / d) * force;
        }
      }
    }

    // Draw links
    const maxD = CONFIG.maxLinkDist;
    const maxD2 = maxD * maxD;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < maxD2) {
          const alpha = (1 - d2 / maxD2) * 0.45;
          ctx.strokeStyle = `rgba(${CONFIG.lineColor[0]}, ${CONFIG.lineColor[1]}, ${CONFIG.lineColor[2]}, ${alpha})`;
          ctx.lineWidth = 0.8;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }

    // Draw nodes
    for (const n of nodes) {
      const glow = (Math.sin(n.pulse) + 1) * 0.5;
      const radius = n.r + glow * 0.8;

      ctx.beginPath();
      ctx.arc(n.x, n.y, radius * 3, 0, Math.PI * 2);
      const grad = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, radius * 3);
      grad.addColorStop(0, `rgba(${CONFIG.nodeColor[0]}, ${CONFIG.nodeColor[1]}, ${CONFIG.nodeColor[2]}, ${0.35 + glow * 0.3})`);
      grad.addColorStop(1, "rgba(93, 242, 255, 0)");
      ctx.fillStyle = grad;
      ctx.fill();

      ctx.beginPath();
      ctx.arc(n.x, n.y, radius, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${CONFIG.nodeColor[0]}, ${CONFIG.nodeColor[1]}, ${CONFIG.nodeColor[2]}, ${0.85})`;
      ctx.fill();
    }

    if (!prefersReduced) requestAnimationFrame(step);
  }

  window.addEventListener("resize", resize, { passive: true });
  window.addEventListener("mousemove", (e) => {
    mouse.x = e.clientX;
    mouse.y = e.clientY;
    mouse.active = true;
  }, { passive: true });
  window.addEventListener("mouseleave", () => { mouse.active = false; });

  resize();
  if (prefersReduced) {
    // Draw one frame only
    step = (() => {
      const once = step;
      return () => { once(); };
    })();
  }
  requestAnimationFrame(step);
})();
