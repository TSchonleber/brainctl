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

// Brain region target positions (spherical-ish layout)
const REGION_CENTERS = {
  prefrontal:  { x: 0, y: 220, z: 80 },    // top — decisions, planning
  temporal_l:  { x: -250, y: -30, z: 50 },  // left — older events
  temporal_r:  { x: 250, y: -30, z: 50 },   // right — recent events
  hippocampus: { x: 0, y: -20, z: 0 },      // center — memories
  cortex_top:  { x: 0, y: 160, z: -120 },   // upper back — concepts
  cortex_left: { x: -200, y: 80, z: -60 },  // left — tools
  cortex_right:{ x: 200, y: 80, z: -60 },   // right — projects
  cortex_front:{ x: 0, y: 40, z: 180 },     // front — people, orgs
  amygdala:    { x: 0, y: -100, z: 60 },     // below center — affect
  agents_l:    { x: -180, y: -140, z: -40 }, // lower left — agents cluster
  agents_r:    { x: 180, y: -140, z: -40 },  // lower right — agents cluster
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
    // Split agents across two clusters by hash
    const hash = (node.label || '').charCodeAt(0) || 0;
    return hash % 2 === 0 ? 'agents_l' : 'agents_r';
  }
  return 'hippocampus';
}

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
  controls.dampingFactor = 0.06;
  controls.rotateSpeed = 0.5;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.3;
  controls.minDistance = 120;
  controls.maxDistance = 2500;

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

  // Initial positions based on brain region + jitter
  nodes.forEach(node => {
    const region = getRegion(node);
    const center = REGION_CENTERS[region] || { x: 0, y: 0, z: 0 };
    const spread = isNarrative(node) ? 80 : 60;
    const sn = {
      ...node,
      x: center.x + (Math.random() - 0.5) * spread,
      y: center.y + (Math.random() - 0.5) * spread,
      z: center.z + (Math.random() - 0.5) * spread,
      vx: 0, vy: 0, vz: 0,
      region,
      size: getNodeSize(node),
    };
    simNodes.push(sn);
    nodeMap[node.id] = sn;
  });

  edges.forEach(edge => {
    const src = nodeMap[edge.source];
    const tgt = nodeMap[edge.target];
    if (src && tgt) simEdges.push({ ...edge, src, tgt });
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

    // Glow
    const glowMat = new THREE.SpriteMaterial({
      map: makeGlow(color), blending: THREE.AdditiveBlending,
      transparent: true, opacity: 0.3
    });
    const glow = new THREE.Sprite(glowMat);
    glow.scale.set(sn.size * 5, sn.size * 5, 1);
    mesh.add(glow);

    // Label for significant nodes
    if (sn.size > 1.8 || !isNarrative(sn)) {
      const label = makeLabel(sn.label || sn.id, color);
      label.position.set(0, -(sn.size + 2.5), 0);
      mesh.add(label);
    }
  });

  // Edges as lines
  simEdges.forEach(edge => {
    const isEntity = edge.kind === 'entity';
    const color = isEntity ? 0x4fc3f7 : 0x223344;
    const opacity = isEntity ? 0.45 : 0.12;
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
    // Pull toward brain region center
    const rc = REGION_CENTERS[a.region] || { x: 0, y: 0, z: 0 };
    a.vx += (rc.x - a.x) * regionPull;
    a.vy += (rc.y - a.y) * regionPull;
    a.vz += (rc.z - a.z) * regionPull;

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

function esc(v) { return v ? String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') : ''; }
