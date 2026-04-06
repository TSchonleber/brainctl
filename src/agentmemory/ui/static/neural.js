/* ===== Neural Map — Brain-Structured 3D Knowledge Visualization ===== */
/* Three.js force-directed graph organized by cognitive function regions */

let neuralInitialized = false;
let scene, camera3d, renderer, controls;
let graphData = { nodes: [], edges: [] };
let nodeMeshes = new Map();
let edgeLines = [];
let impulseParticles = [];
let simNodes = [];
let simEdges = [];
let raycaster, mouse;
let hoveredNode = null;
let selectedNode = null;
let neuralFilters = { scope: 'overview', showEvents: true, showDecisions: true, showMemories: true };
let clock;

const NODE_COLORS = {
  person: 0x4fc3f7, project: 0x81c784, tool: 0xffb74d,
  concept: 0xce93d8, agent: 0x5c6bc0, organization: 0x4dd0e1,
  decision: 0xffd54f, event: 0x4dd0e1, memory: 0x66bb6a, unknown: 0x888888
};

const REGION_COLORS = {
  prefrontal: 0xffd54f,  // decisions — gold
  temporal: 0x4dd0e1,    // events — cyan
  hippocampus: 0x66bb6a, // memories — green
  cortex: 0x7986cb,      // entities — indigo
  amygdala: 0xef5350,    // affect — red
};

// Brain region target positions (organic layout, no symmetric balls)
const REGION_CENTERS = {
  prefrontal:  { x: 0, y: 200, z: 60 },     // top — decisions, planning
  temporal_l:  { x: -220, y: -20, z: 40 },   // left — older events
  temporal_r:  { x: 220, y: -20, z: 40 },    // right — recent events
  hippocampus: { x: 0, y: 0, z: 0 },         // center — memories (core)
  cortex_top:  { x: 0, y: 150, z: -100 },    // upper back — concepts
  cortex_left: { x: -170, y: 80, z: -50 },   // left — tools
  cortex_right:{ x: 170, y: 80, z: -50 },    // right — projects
  cortex_front:{ x: 0, y: 60, z: 160 },      // front — people, orgs
  amygdala:    { x: 0, y: -80, z: 40 },      // below center — affect
  periphery:   { x: 0, y: -150, z: -80 },    // far back — unconnected agents (tiny, out of way)
};

function getRegion(node) {
  const kind = node.kind || node.type;
  if (kind === 'decision') return 'prefrontal';
  if (kind === 'event') {
    // Split events left/right by age
    const created = node.created_at || '';
    return created > '2026-04-01' ? 'temporal_r' : 'temporal_l';
  }
  if (kind === 'memory') return 'hippocampus';
  if (kind === 'concept') return 'cortex_top';
  if (kind === 'tool') return 'cortex_left';
  if (kind === 'project') return 'cortex_right';
  if (kind === 'person' || kind === 'organization') return 'cortex_front';
  if (kind === 'agent') {
    // Connected agents get positioned by force-directed pull to their content.
    // Orphan agents go to periphery as tiny dust.
    return 'periphery';
  }
  return 'hippocampus';
}

// Track which agents have edges (set during buildScene)
let connectedAgentIds = new Set();

function getRegionColor(node) {
  const kind = node.kind || node.type;
  if (kind === 'decision') return REGION_COLORS.prefrontal;
  if (kind === 'event') return REGION_COLORS.temporal;
  if (kind === 'memory') return REGION_COLORS.hippocampus;
  if (kind === 'agent') return 0x5c6bc0;
  return NODE_COLORS[kind] || NODE_COLORS.unknown;
}

function getNodeSize(node) {
  const kind = node.kind || node.type;
  const base = {
    person: 4.0, organization: 3.5, project: 3.0, tool: 2.5,
    concept: 2.8, decision: 2.2, event: 1.4, memory: 1.6, agent: 1.0,
  }[kind] || 1.5;
  const conf = node.confidence || 0.5;
  return base * (0.6 + conf * 0.5);
}

// ===== THREE.JS LOADING =====
function loadThreeJS() {
  return new Promise((resolve, reject) => {
    if (window.THREE && window.THREE.OrbitControls) { resolve(); return; }
    const s1 = document.createElement('script');
    s1.src = 'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js';
    s1.onload = () => {
      const s2 = document.createElement('script');
      s2.src = 'https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js';
      s2.onload = () => resolve();
      s2.onerror = () => reject('OrbitControls failed');
      document.head.appendChild(s2);
    };
    s1.onerror = () => reject('Three.js failed');
    document.head.appendChild(s1);
  });
}

// ===== INIT =====
async function initNeural() {
  if (neuralInitialized) return;
  neuralInitialized = true;

  await loadThreeJS();
  const container = document.getElementById('neuralView');
  const canvas = document.getElementById('neuralCanvas');
  clock = new THREE.Clock();

  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x050a14);
  scene.fog = new THREE.FogExp2(0x050a14, 0.0006);

  camera3d = new THREE.PerspectiveCamera(55, container.clientWidth / container.clientHeight, 1, 8000);
  camera3d.position.set(0, 150, 700);

  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.2;

  controls = new THREE.OrbitControls(camera3d, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;
  controls.rotateSpeed = 0.4;
  controls.panSpeed = 0.8;
  controls.zoomSpeed = 0.8;
  controls.enablePan = true;       // right-click + drag to pan
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.25;
  controls.minDistance = 60;
  controls.maxDistance = 2000;
  controls.mouseButtons = {
    LEFT: THREE.MOUSE.ROTATE,
    MIDDLE: THREE.MOUSE.DOLLY,
    RIGHT: THREE.MOUSE.PAN
  };
  // Smooth zoom with scroll
  controls.enableZoom = true;

  // Lighting
  scene.add(new THREE.AmbientLight(0x1a1a3e, 0.5));
  const hemi = new THREE.HemisphereLight(0x4fc3f7, 0x1a0a2e, 0.3);
  scene.add(hemi);
  const point1 = new THREE.PointLight(0x4fc3f7, 0.6, 1200);
  point1.position.set(200, 300, 200);
  scene.add(point1);
  const point2 = new THREE.PointLight(0xce93d8, 0.4, 800);
  point2.position.set(-200, -100, -200);
  scene.add(point2);

  // Starfield
  const starGeo = new THREE.BufferGeometry();
  const starPos = new Float32Array(4500);
  for (let i = 0; i < 4500; i++) starPos[i] = (Math.random() - 0.5) * 5000;
  starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
  scene.add(new THREE.Points(starGeo, new THREE.PointsMaterial({
    color: 0x4fc3f7, size: 1.0, transparent: true, opacity: 0.2
  })));

  // Brain region markers (subtle glowing spheres showing structure)
  Object.entries(REGION_CENTERS).forEach(([name, pos]) => {
    if (name.startsWith('agents')) return; // skip agent clusters
    const color = name === 'prefrontal' ? REGION_COLORS.prefrontal :
                  name.startsWith('temporal') ? REGION_COLORS.temporal :
                  name === 'hippocampus' ? REGION_COLORS.hippocampus :
                  name === 'amygdala' ? REGION_COLORS.amygdala : REGION_COLORS.cortex;
    const marker = new THREE.Mesh(
      new THREE.SphereGeometry(35, 24, 16),
      new THREE.MeshPhongMaterial({ color, transparent: true, opacity: 0.04, emissive: color, emissiveIntensity: 0.15 })
    );
    marker.position.set(pos.x, pos.y, pos.z);
    scene.add(marker);
  });

  raycaster = new THREE.Raycaster();
  mouse = new THREE.Vector2();

  renderer.domElement.addEventListener('mousemove', onMouseMove3D);
  renderer.domElement.addEventListener('click', onClick3D);
  window.addEventListener('resize', onResize3D);

  // Filter buttons — with actual different behavior
  document.querySelectorAll('.neural-filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      neuralFilters.scope = btn.dataset.filter;
      document.querySelectorAll('.neural-filter-btn').forEach(el => el.classList.toggle('active', el === btn));
      buildScene();
    });
  });
  bindToggle('neuralEventsToggle', 'showEvents');
  bindToggle('neuralDecisionsToggle', 'showDecisions');
  bindToggle('neuralMemoriesToggle', 'showMemories');

  document.getElementById('neuralZoomIn').addEventListener('click', () => camera3d.position.multiplyScalar(0.75));
  document.getElementById('neuralZoomOut').addEventListener('click', () => camera3d.position.multiplyScalar(1.35));
  document.getElementById('neuralReset').addEventListener('click', () => {
    camera3d.position.set(0, 150, 700);
    controls.target.set(0, 0, 0);
    controls.autoRotate = true;
    selectedNode = null;
    hideDetail();
  });

  await loadGraph();
  startActivityFeed();
  animate();
}

function bindToggle(id, key) {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener('change', (e) => { neuralFilters[key] = e.target.checked; buildScene(); });
}

async function loadGraph() {
  try {
    const res = await fetch('/api/graph');
    graphData = await res.json();
    buildScene();
  } catch (err) { console.error('Graph load error:', err); }
}

function isNarrative(n) { return n.kind === 'event' || n.kind === 'decision' || n.kind === 'memory'; }

// ===== FILTERING — Each mode shows genuinely different data =====
function getFilteredData() {
  const allNodes = graphData.nodes || [];
  const allEdges = graphData.edges || [];

  // Toggle filters
  let nodes = allNodes.filter(n => {
    if (n.kind === 'event' && !neuralFilters.showEvents) return false;
    if (n.kind === 'decision' && !neuralFilters.showDecisions) return false;
    if (n.kind === 'memory' && !neuralFilters.showMemories) return false;
    return true;
  });

  if (neuralFilters.scope === 'overview') {
    // OVERVIEW: structural backbone only — entities + their inter-entity edges
    // No events, no memories, no decisions. Pure knowledge graph.
    nodes = nodes.filter(n => !isNarrative(n));
  } else if (neuralFilters.scope === 'thinking') {
    // THINKING: entities + decisions + recent events (active cognition)
    // Skip memories (long-term storage) and old events (processed)
    nodes = nodes.filter(n => {
      if (n.kind === 'memory') return false;
      if (n.kind === 'event') {
        // Only last 30 events
        const rank = allNodes.filter(x => x.kind === 'event')
          .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
          .indexOf(n);
        return rank < 30;
      }
      return true;
    });
  }
  // 'all' = everything

  const nodeIds = new Set(nodes.map(n => n.id));
  const edges = allEdges.filter(e => nodeIds.has(e.source) && nodeIds.has(e.target));
  return { nodes, edges };
}

// ===== BUILD SCENE =====
function buildScene() {
  // Clear
  nodeMeshes.forEach(m => scene.remove(m));
  nodeMeshes.clear();
  edgeLines.forEach(l => scene.remove(l));
  edgeLines = [];
  impulseParticles.forEach(p => scene.remove(p.mesh));
  impulseParticles = [];
  simNodes = [];
  simEdges = [];

  const { nodes, edges } = getFilteredData();
  const nodeMap = {};

  // Pre-compute which agents have edges
  connectedAgentIds = new Set();
  edges.forEach(e => {
    const srcNode = nodes.find(n => n.id === e.source);
    const tgtNode = nodes.find(n => n.id === e.target);
    if (srcNode && (srcNode.kind === 'agent' || srcNode.type === 'agent')) connectedAgentIds.add(srcNode.id);
    if (tgtNode && (tgtNode.kind === 'agent' || tgtNode.type === 'agent')) connectedAgentIds.add(tgtNode.id);
  });

  // Hide orphan agents completely — they're visual noise with no information
  let visibleNodes = nodes.filter(n => {
    const kind = n.kind || n.type;
    if (kind === 'agent' && !connectedAgentIds.has(n.id)) return false;
    return true;
  });

  // Initial positions based on brain region + jitter
  visibleNodes.forEach(node => {
    const kind = node.kind || node.type;
    const isOrphanAgent = kind === 'agent' && !connectedAgentIds.has(node.id);
    const region = getRegion(node);
    const center = REGION_CENTERS[region] || { x: 0, y: 0, z: 0 };

    const spread = isNarrative(node) ? 100 : 70; // wider spread to reduce center blob
    const sn = {
      ...node,
      x: center.x + (Math.random() - 0.5) * spread,
      y: center.y + (Math.random() - 0.5) * spread,
      z: center.z + (Math.random() - 0.5) * spread,
      vx: 0, vy: 0, vz: 0,
      region,
      size: getNodeSize(node),
      isOrphan: false,
    };
    simNodes.push(sn);
    nodeMap[node.id] = sn;
  });

  edges.forEach(edge => {
    const src = nodeMap[edge.source];
    const tgt = nodeMap[edge.target];
    if (src && tgt) simEdges.push({ ...edge, src, tgt });
  });

  // Override region for connected agents — let edges pull them to their content
  simNodes.forEach(sn => {
    if ((sn.kind === 'agent' || sn.type === 'agent') && connectedAgentIds.has(sn.id)) {
      // Position near center initially, let force-directed layout pull them to their edges
      sn.region = 'hippocampus'; // central, gets pulled by edge forces
      sn.x = (Math.random() - 0.5) * 100;
      sn.y = (Math.random() - 0.5) * 100;
      sn.z = (Math.random() - 0.5) * 100;
    }
  });

  // Run force simulation
  for (let i = 0; i < 250; i++) stepSim();

  // Create meshes
  simNodes.forEach(sn => {
    const color = getRegionColor(sn);
    const geo = new THREE.SphereGeometry(sn.size, 14, 10);
    const mat = new THREE.MeshPhongMaterial({
      color, emissive: color, emissiveIntensity: 0.35,
      transparent: true, opacity: isNarrative(sn) ? 0.8 : 0.95,
      shininess: 60
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.set(sn.x, sn.y, sn.z);
    mesh.userData = sn;
    scene.add(mesh);
    nodeMeshes.set(sn.id, mesh);

    // Glow — subtle for orphans, full for connected nodes
    const glowMat = new THREE.SpriteMaterial({
      map: makeGlow(color), blending: THREE.AdditiveBlending,
      transparent: true, opacity: sn.isOrphan ? 0.08 : 0.3
    });
    const glow = new THREE.Sprite(glowMat);
    glow.scale.set(sn.size * (sn.isOrphan ? 3 : 5), sn.size * (sn.isOrphan ? 3 : 5), 1);
    mesh.add(glow);

    // Label — show on all non-orphan nodes
    if (!sn.isOrphan) {
      const labelText = (sn.label || sn.id).substring(0, 60);
      const label = makeLabel(labelText, color);
      label.position.set(0, -(sn.size + 2.5), 0);
      mesh.add(label);
    }
  });

  // Edges as lines — authored_by connections are prominent
  simEdges.forEach(edge => {
    const isEntity = edge.kind === 'entity';
    const isAuthored = edge.kind === 'authored_by';
    const isDecision = edge.kind === 'decision';
    const isMemory = edge.kind === 'memory';
    const color = isEntity ? 0x4fc3f7 : isAuthored ? 0x7c4dff : isDecision ? 0xffd54f : isMemory ? 0x66bb6a : 0x1a2a3a;
    const opacity = isEntity ? 0.5 : isAuthored ? 0.55 : isDecision ? 0.4 : isMemory ? 0.35 : 0.08;
    const geo = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(edge.src.x, edge.src.y, edge.src.z),
      new THREE.Vector3(edge.tgt.x, edge.tgt.y, edge.tgt.z),
    ]);
    const line = new THREE.Line(geo, new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
    line.userData = edge;
    scene.add(line);
    edgeLines.push(line);

    // Neural impulse particles on entity edges
    if (isEntity && Math.random() < 0.6) {
      const pGeo = new THREE.SphereGeometry(0.5, 6, 4);
      const pMat = new THREE.MeshBasicMaterial({ color: 0x4fc3f7, transparent: true, opacity: 0.8 });
      const pMesh = new THREE.Mesh(pGeo, pMat);
      scene.add(pMesh);
      impulseParticles.push({ mesh: pMesh, edge, t: Math.random(), speed: 0.002 + Math.random() * 0.004 });
    }
  });
}

// ===== FORCE SIMULATION =====
function stepSim() {
  const repulsion = 60;
  const attraction = 0.006;
  const regionPull = 0.003; // Pull toward assigned brain region
  const damping = 0.82;

  for (let i = 0; i < simNodes.length; i++) {
    const a = simNodes[i];
    // Pull toward brain region center (weaker for connected agents — edges dominate)
    const rc = REGION_CENTERS[a.region] || { x: 0, y: 0, z: 0 };
    const isConnAgent = (a.kind === 'agent' || a.type === 'agent') && connectedAgentIds.has(a.id);
    const rp = isConnAgent ? regionPull * 0.15 : regionPull; // agents: mostly edge-positioned
    a.vx += (rc.x - a.x) * rp;
    a.vy += (rc.y - a.y) * rp;
    a.vz += (rc.z - a.z) * rp;

    for (let j = i + 1; j < simNodes.length; j++) {
      const b = simNodes[j];
      let dx = b.x - a.x, dy = b.y - a.y, dz = b.z - a.z;
      let dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
      if (dist > 400) continue;
      // Stronger repulsion within same region
      const sameRegion = a.region === b.region ? 1.5 : 1.0;
      const force = (repulsion * sameRegion) / (dist * dist);
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      const fz = (dz / dist) * force;
      a.vx -= fx; a.vy -= fy; a.vz -= fz;
      b.vx += fx; b.vy += fy; b.vz += fz;
    }
  }

  simEdges.forEach(edge => {
    const dx = edge.tgt.x - edge.src.x;
    const dy = edge.tgt.y - edge.src.y;
    const dz = edge.tgt.z - edge.src.z;
    const dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
    const ideal = edge.kind === 'entity' ? 40 : 70;
    const force = (dist - ideal) * attraction;
    const fx = (dx / dist) * force;
    const fy = (dy / dist) * force;
    const fz = (dz / dist) * force;
    edge.src.vx += fx; edge.src.vy += fy; edge.src.vz += fz;
    edge.tgt.vx -= fx; edge.tgt.vy -= fy; edge.tgt.vz -= fz;
  });

  simNodes.forEach(n => {
    n.vx *= damping; n.vy *= damping; n.vz *= damping;
    n.x += n.vx; n.y += n.vy; n.z += n.vz;
  });
}

// ===== TEXTURES =====
function makeGlow(color) {
  const c = document.createElement('canvas');
  c.width = 64; c.height = 64;
  const ctx = c.getContext('2d');
  const hex = '#' + new THREE.Color(color).getHexString();
  const g = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
  g.addColorStop(0, hex); g.addColorStop(0.25, hex + '66'); g.addColorStop(1, hex + '00');
  ctx.fillStyle = g; ctx.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
}

function makeLabel(text, color) {
  const c = document.createElement('canvas');
  const ctx = c.getContext('2d');
  const label = (text || '').substring(0, 28);
  ctx.font = 'bold 22px system-ui, sans-serif';
  const w = Math.min(ctx.measureText(label).width + 12, 360);
  c.width = w; c.height = 32;
  ctx.font = 'bold 22px system-ui, sans-serif';
  ctx.fillStyle = '#' + new THREE.Color(color).getHexString();
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(label, w / 2, 16);
  const mat = new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), transparent: true, opacity: 0.8 });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(w / 12, 2.8, 1);
  return sprite;
}

// ===== ANIMATE =====
function animate() {
  requestAnimationFrame(animate);
  const dt = clock.getDelta();
  controls.update();

  // Gentle continuous simulation
  stepSim();

  // Update positions with lerp
  simNodes.forEach(sn => {
    const mesh = nodeMeshes.get(sn.id);
    if (mesh) mesh.position.lerp(new THREE.Vector3(sn.x, sn.y, sn.z), 0.04);
  });

  // Update edges
  edgeLines.forEach(line => {
    const e = line.userData;
    const p = line.geometry.attributes.position.array;
    p[0] = e.src.x; p[1] = e.src.y; p[2] = e.src.z;
    p[3] = e.tgt.x; p[4] = e.tgt.y; p[5] = e.tgt.z;
    line.geometry.attributes.position.needsUpdate = true;
  });

  // Animate impulse particles along edges (neural firing)
  impulseParticles.forEach(ip => {
    ip.t += ip.speed;
    if (ip.t > 1) ip.t = 0;
    const e = ip.edge;
    ip.mesh.position.set(
      e.src.x + (e.tgt.x - e.src.x) * ip.t,
      e.src.y + (e.tgt.y - e.src.y) * ip.t,
      e.src.z + (e.tgt.z - e.src.z) * ip.t,
    );
    ip.mesh.material.opacity = 0.4 + Math.sin(ip.t * Math.PI) * 0.5;
  });

  // Pulse selected node
  if (selectedNode) {
    const mesh = nodeMeshes.get(selectedNode.id);
    if (mesh) {
      const pulse = 0.3 + Math.sin(clock.elapsedTime * 3) * 0.15;
      mesh.material.emissiveIntensity = pulse;
    }
  }

  // Dim unconnected nodes when something is selected
  nodeMeshes.forEach((mesh, id) => {
    const isS = selectedNode && selectedNode.id === id;
    const isH = hoveredNode && hoveredNode.id === id;
    const isConnected = selectedNode && simEdges.some(e =>
      (e.src.id === selectedNode.id && e.tgt.id === id) ||
      (e.tgt.id === selectedNode.id && e.src.id === id));
    const base = isNarrative(mesh.userData) ? 0.8 : 0.95;
    mesh.material.opacity = isS ? 1.0 : isH ? 0.95 : (selectedNode ? (isConnected ? 0.85 : 0.08) : base);
    if (!isS) mesh.material.emissiveIntensity = isH ? 0.5 : (selectedNode && !isConnected ? 0.05 : 0.35);
  });

  // Birth/death particle effects
  animateBirthEffects(dt);

  renderer.render(scene, camera3d);
}

// ===== INTERACTION =====
function onResize3D() {
  const c = document.getElementById('neuralView');
  if (!c || !renderer) return;
  camera3d.aspect = c.clientWidth / c.clientHeight;
  camera3d.updateProjectionMatrix();
  renderer.setSize(c.clientWidth, c.clientHeight);
}

function onMouseMove3D(event) {
  const rect = renderer.domElement.getBoundingClientRect();
  mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(mouse, camera3d);
  const hits = raycaster.intersectObjects(Array.from(nodeMeshes.values()));
  hoveredNode = hits.length > 0 ? hits[0].object.userData : null;
  renderer.domElement.style.cursor = hoveredNode ? 'pointer' : 'grab';

  const tooltip = document.getElementById('neuralTooltip');
  if (hoveredNode) {
    const hex = '#' + new THREE.Color(getRegionColor(hoveredNode)).getHexString();
    const kind = hoveredNode.kind || hoveredNode.type || 'node';
    const meta = [hoveredNode.agent_id, hoveredNode.project, hoveredNode.event_type].filter(Boolean);
    const region = getRegion(hoveredNode).replace(/_/g, ' ');
    tooltip.innerHTML = `
      <div class="tooltip-type" style="color:${hex}">${esc(kind)} · ${esc(region)}</div>
      <div class="tooltip-name">${esc(hoveredNode.label)}</div>
      ${meta.length ? `<div class="tooltip-meta">${esc(meta.join(' · '))}</div>` : ''}
    `;
    tooltip.classList.remove('hidden');
    tooltip.style.left = `${event.clientX + 16}px`;
    tooltip.style.top = `${event.clientY - 20}px`;
  } else {
    tooltip.classList.add('hidden');
  }
}

function onClick3D(event) {
  if (!hoveredNode) { selectedNode = null; hideDetail(); return; }
  selectedNode = hoveredNode;
  controls.autoRotate = false; // stop spinning when user selects
  // Fly toward node
  const t = new THREE.Vector3(selectedNode.x, selectedNode.y, selectedNode.z);
  controls.target.lerp(t, 0.5);
  showDetail(selectedNode);
}

function showDetail(node) {
  const panel = document.getElementById('neuralDetail');
  const hex = '#' + new THREE.Color(getRegionColor(node)).getHexString();
  const kind = node.kind || node.type || 'node';
  const region = getRegion(node).replace(/_/g, ' ');
  const conns = simEdges
    .filter(e => e.src.id === node.id || e.tgt.id === node.id)
    .slice(0, 20)
    .map(e => {
      const other = e.src.id === node.id ? e.tgt : e.src;
      return `<li>${esc(e.label || e.kind || 'linked')}: <span style="color:${hex}">${esc(other.label)}</span></li>`;
    }).join('');

  const facts = [];
  if (node.agent_id) facts.push(`Agent: ${node.agent_id}`);
  if (node.project) facts.push(`Project: ${node.project}`);
  if (node.event_type) facts.push(`Event type: ${node.event_type}`);
  if (node.created_at) facts.push(`Created: ${node.created_at}`);
  facts.push(`Region: ${region}`);

  let body = '';
  if (node.detail) body = `<div class="detail-section-title">Content</div><div class="detail-text">${esc(node.detail)}</div>`;
  else if (node.observations && node.observations.length)
    body = `<div class="detail-section-title">Observations</div><ul class="detail-obs-list">${node.observations.map(o => `<li>${esc(o)}</li>`).join('')}</ul>`;

  const edgeCount = simEdges.filter(e => e.src.id === node.id || e.tgt.id === node.id).length;

  panel.innerHTML = `
    <button class="detail-close" onclick="hideDetail();selectedNode=null;controls.autoRotate=true">×</button>
    <div class="detail-name" style="color:${hex}">${esc(node.label)}</div>
    <div class="detail-type">${esc(kind)} · ${esc(region)}</div>
    ${facts.length ? `<ul class="detail-obs-list">${facts.map(f => `<li>${esc(f)}</li>`).join('')}</ul>` : ''}
    ${body}
    ${conns ? `<div class="detail-section-title">Connections (${edgeCount})</div><ul class="detail-obs-list">${conns}</ul>` : ''}
  `;
  panel.classList.remove('hidden');
}

function hideDetail() { document.getElementById('neuralDetail').classList.add('hidden'); }

// ===== LIVE ACTIVITY FEED — poll for new data every 3s =====
let lastActivityTs = new Date().toISOString().replace('Z','').slice(0,19);
let activityInterval = null;
let birthEffects = []; // nodes being born (scale up animation)
let deathEffects = []; // nodes being retired (fade out)

function startActivityFeed() {
  if (activityInterval) return;
  activityInterval = setInterval(pollActivity, 3000);
}

async function pollActivity() {
  try {
    const res = await fetch(`/api/activity?since=${encodeURIComponent(lastActivityTs)}`);
    const data = await res.json();

    let hasNew = false;

    // New events — flash a burst at the temporal region
    if (data.events && data.events.length > 0) {
      data.events.forEach(ev => {
        spawnBurst(REGION_CENTERS.temporal_r, REGION_COLORS.temporal, 8);
      });
      hasNew = true;
    }

    // New memories — flash at hippocampus
    if (data.new_memories && data.new_memories.length > 0) {
      data.new_memories.forEach(m => {
        spawnBurst(REGION_CENTERS.hippocampus, REGION_COLORS.hippocampus, 12);
      });
      hasNew = true;
    }

    // New edges — flash impulse along a random existing edge
    if (data.new_edges && data.new_edges.length > 0) {
      data.new_edges.forEach(() => {
        if (simEdges.length > 0) {
          const edge = simEdges[Math.floor(Math.random() * simEdges.length)];
          spawnImpulse(edge, 0x4fc3f7, 2.0);
        }
      });
      hasNew = true;
    }

    // Retirements — flash red at hippocampus
    if (data.retirements && data.retirements.length > 0) {
      data.retirements.forEach(() => {
        spawnBurst(REGION_CENTERS.hippocampus, 0xef5350, 6);
      });
      hasNew = true;
    }

    // Affect changes — flash at amygdala
    if (data.affect && data.affect.length > 0) {
      data.affect.forEach(a => {
        const color = a.valence > 0 ? 0x66bb6a : a.valence < -0.3 ? 0xef5350 : 0xffd54f;
        spawnBurst(REGION_CENTERS.amygdala, color, 10);
      });
      hasNew = true;
    }

    // Update activity log display
    if (hasNew) {
      updateActivityLog(data);
    }

    // Advance timestamp
    const allTimes = [
      ...(data.events || []).map(e => e.created_at),
      ...(data.new_memories || []).map(m => m.created_at),
      ...(data.affect || []).map(a => a.created_at),
    ].filter(Boolean).sort();
    if (allTimes.length > 0) {
      lastActivityTs = allTimes[allTimes.length - 1];
    }
  } catch (err) {
    // Polling failure is silent
  }
}

function spawnBurst(center, color, count) {
  if (!scene) return;
  for (let i = 0; i < count; i++) {
    const geo = new THREE.SphereGeometry(0.8, 6, 4);
    const mat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9 });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.set(
      center.x + (Math.random() - 0.5) * 20,
      center.y + (Math.random() - 0.5) * 20,
      center.z + (Math.random() - 0.5) * 20,
    );
    scene.add(mesh);
    birthEffects.push({
      mesh, age: 0, maxAge: 1.5 + Math.random(),
      vx: (Math.random() - 0.5) * 40,
      vy: (Math.random() - 0.5) * 40,
      vz: (Math.random() - 0.5) * 40,
    });
  }
}

function spawnImpulse(edge, color, size) {
  if (!scene) return;
  const geo = new THREE.SphereGeometry(size, 6, 4);
  const mat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 1.0 });
  const mesh = new THREE.Mesh(geo, mat);
  scene.add(mesh);
  impulseParticles.push({ mesh, edge, t: 0, speed: 0.015 + Math.random() * 0.01 });
}

function updateActivityLog(data) {
  // Show a small activity feed overlay
  let log = document.getElementById('activityLog');
  if (!log) {
    log = document.createElement('div');
    log.id = 'activityLog';
    log.style.cssText = 'position:absolute;bottom:12px;left:12px;max-width:340px;max-height:200px;overflow:hidden;font:11px system-ui;color:#8af;pointer-events:none;z-index:50;';
    document.getElementById('neuralView').appendChild(log);
  }
  const items = [];
  (data.events || []).slice(0, 3).forEach(e => {
    items.push(`<div style="opacity:0.7;margin:2px 0">⚡ ${esc((e.summary||'').slice(0,80))}</div>`);
  });
  (data.new_memories || []).slice(0, 2).forEach(m => {
    items.push(`<div style="opacity:0.7;margin:2px 0;color:#6b6">💭 ${esc((m.content||'').slice(0,80))}</div>`);
  });
  (data.retirements || []).slice(0, 2).forEach(r => {
    items.push(`<div style="opacity:0.7;margin:2px 0;color:#e55">🗑 retired: ${esc((r.content||'').slice(0,60))}</div>`);
  });
  (data.affect || []).slice(0, 2).forEach(a => {
    const c = a.valence > 0 ? '#6b6' : a.valence < -0.3 ? '#e55' : '#fd5';
    items.push(`<div style="opacity:0.7;margin:2px 0;color:${c}">🧠 ${esc(a.agent_id)}: ${esc(a.affect_label)} (v=${a.valence?.toFixed(2)})</div>`);
  });
  if (items.length > 0) {
    log.innerHTML = items.join('');
    log.style.opacity = '1';
    setTimeout(() => { log.style.opacity = '0.3'; }, 4000);
  }
}

// Birth/death effect animations (called from main animate loop)
function animateBirthEffects(dt) {
  for (let i = birthEffects.length - 1; i >= 0; i--) {
    const b = birthEffects[i];
    b.age += dt;
    b.mesh.position.x += b.vx * dt;
    b.mesh.position.y += b.vy * dt;
    b.mesh.position.z += b.vz * dt;
    b.mesh.material.opacity = Math.max(0, 1 - b.age / b.maxAge);
    const s = 0.5 + b.age * 2;
    b.mesh.scale.set(s, s, s);
    if (b.age > b.maxAge) {
      scene.remove(b.mesh);
      birthEffects.splice(i, 1);
    }
  }
  // Clean up finished impulses
  for (let i = impulseParticles.length - 1; i >= 0; i--) {
    if (impulseParticles[i].t > 1) {
      scene.remove(impulseParticles[i].mesh);
      impulseParticles.splice(i, 1);
    }
  }
}

// (activity feed started inside initNeural after loadGraph)
