const $ = (id) => document.getElementById(id);
let scene, camera, renderer, controls, THREE;
let currentModel = null;
let evidenceImg = null;
let evidenceBounds = null;
let highlightedBarId = null;
let barMeshMap = new Map();   // mesh uuid -> bar component id
let barIdList = [];           // 与 GLB mesh 顺序对齐
let currentModelPath = null;
let raycaster, mouse;

function initViewer() {
  try {
    const container = $('viewer');
    import('three').then((T) => {
      THREE = T;
      scene = new THREE.Scene();
      scene.background = new THREE.Color(0x0e1116);
      camera = new THREE.PerspectiveCamera(50, container.clientWidth / container.clientHeight, 1, 50000);
      camera.position.set(4000, 5000, 12000);
      renderer = new THREE.WebGLRenderer({ antialias: true });
      renderer.setSize(container.clientWidth, container.clientHeight);
      container.appendChild(renderer.domElement);
      scene.add(new THREE.AmbientLight(0xffffff, 0.9));
      const dir = new THREE.DirectionalLight(0xffffff, 1.2);
      dir.position.set(1, 2, 1);
      scene.add(dir);
      raycaster = new THREE.Raycaster();
      mouse = new THREE.Vector2();
      renderer.domElement.addEventListener('click', onViewerClick);
      import('three/addons/controls/OrbitControls.js').then(({ OrbitControls }) => {
        controls = new OrbitControls(camera, renderer.domElement);
        controls.update();
        animate();
      });
    });
  } catch (e) {
    $('viewer').textContent = '3D 预览依赖 three.js CDN（需联网）：' + e;
  }
}

function animate() {
  requestAnimationFrame(animate);
  if (controls) controls.update();
  if (renderer && scene && camera) renderer.render(scene, camera);
}

function onViewerClick(ev) {
  if (!renderer || !camera || !currentModel) return;
  const rect = renderer.domElement.getBoundingClientRect();
  mouse.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
  mouse.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects(scene.children, true);
  for (const hit of hits) {
    const barId = hit.object.userData?.barId || barMeshMap.get(hit.object.uuid);
    if (barId && currentModel.components[barId]) {
      $('status').textContent = `3D→2D 选中：${barId}`;
      highlightBar2D(barId, currentModel.components[barId]);
      return;
    }
  }
}

function loadGlb(url) {
  if (!scene || !renderer || !THREE) return;
  import('three/addons/loaders/GLTFLoader.js').then(({ GLTFLoader }) => {
    new GLTFLoader().load(url, (gltf) => {
      const old = scene.getObjectByName('tower');
      if (old) scene.remove(old);
      gltf.scene.name = 'tower';
      barMeshMap.clear();
      gltf.scene.traverse((obj) => {
        if (!obj.isMesh) return;
        const extras = obj.userData || {};
        const compId = extras.component_id || extras.componentId;
        const barId = extras.bar_id || extras.barId;
        if (compId) {
          obj.userData.barId = compId;
          obj.userData.bar_id = barId || compId;
          barMeshMap.set(obj.uuid, compId);
        }
      });
      // 回退：按 mesh 顺序对齐 barIdList（旧 GLB 无 extras 时）
      if (barMeshMap.size === 0) {
        let meshIdx = 0;
        gltf.scene.traverse((obj) => {
          if (!obj.isMesh) return;
          const compId = barIdList[meshIdx];
          if (compId) {
            obj.userData.barId = compId;
            barMeshMap.set(obj.uuid, compId);
          }
          meshIdx += 1;
        });
      }
      const box = new THREE.Box3().setFromObject(gltf.scene);
      const c = box.getCenter(new THREE.Vector3());
      const s = box.getSize(new THREE.Vector3()).length() / 2 || 1;
      camera.position.copy(c).add(new THREE.Vector3(s * 0.7, s * 0.8, s * 1.2));
      controls.target.copy(c);
      scene.add(gltf.scene);
    }, undefined, (err) => {
      $('status').textContent += '\nGLB 加载失败：' + err;
    });
  });
}

function renderSteps(steps) {
  const tbody = $('steps').querySelector('tbody');
  tbody.innerHTML = '';
  (steps || []).forEach((s) => {
    const tr = document.createElement('tr');
    const cls = s.status === 'passed' ? 'passed' : s.status === 'failed' ? 'failed' : s.status === 'running' ? 'running' : '';
    tr.innerHTML = `<td>${s.id}</td><td>${s.name}</td><td class="${cls}">${s.status}</td>` +
      `<td>${s.duration_ms != null ? s.duration_ms + 'ms' : '-'}</td><td>${s.error || '-'}</td>`;
    tbody.appendChild(tr);
  });
}

async function renderBars(modelPath) {
  const res = await fetch(modelPath);
  const model = await res.json();
  currentModel = model;
  currentModelPath = modelPath;
  const bars = Object.entries(model.components || {}).filter(([, c]) => c.kind === 'tower_bar');
  barIdList = bars.map(([id]) => id);
  const box = $('bars');
  box.innerHTML = '';
  bars.slice(0, 500).forEach(([id, c]) => {
    const div = document.createElement('div');
    div.className = 'bar-item';
    div.dataset.barId = id;
    const src = c.source ? `${c.source.reference || ''} · ${c.source.detail || ''} · conf=${c.source.confidence || 0}` : '无来源';
    const bid = (c.properties || {}).bar_id || id;
    const pending = (c.properties || {}).solve_status === 'pending_review' ? ' [待复核]' : '';
    div.innerHTML = `<b>${bid}</b>${pending} <span class="src">${src}</span>`;
    div.onclick = () => {
      $('status').textContent = `2D→3D 追溯：${id}\n${src}\n${JSON.stringify(c.properties || {}, null, 2)}`;
      highlightBar2D(id, c);
      highlightBar3D(id);
    };
    box.appendChild(div);
  });
  computeEvidenceBounds();
  updateConfirmButton();
  updateDerivedYButton();
  showDerivedYStatus(model);
}

function derivedYPendingNodes(model) {
  return Object.entries(model.components || {}).filter(([, c]) =>
    c.kind === 'tower_node' &&
    c.properties?.y_origin === 'z_peer_interpolate' &&
    c.properties?.y_review !== 'verified',
  );
}

function showDerivedYStatus(model) {
  const pending = derivedYPendingNodes(model);
  const box = $('derived-y-status');
  if (!box) return;
  if (pending.length === 0) {
    const any = Object.values(model.components || {}).some(
      (c) => c.kind === 'tower_node' && c.properties?.y_origin === 'z_peer_interpolate',
    );
    box.textContent = any
      ? '插值 y 已全部复核（y_review=verified）'
      : '无 z-peer 插值 y 节点';
    box.className = 'status derived-y-ok';
    return;
  }
  const ids = pending.map(([id]) => id).slice(0, 8).join(', ');
  box.textContent = `${pending.length} 个节点 y 为 z-peer 插值，待复核：${ids}${pending.length > 8 ? '…' : ''}`;
  box.className = 'status derived-y-pending';
}

function updateDerivedYButton() {
  const btn = $('confirm-derived-y');
  if (!btn || !currentModel) return;
  const pending = derivedYPendingNodes(currentModel);
  btn.disabled = pending.length === 0 || !currentModelPath;
}

function highlightBar3D(barId) {
  if (!scene) return;
  scene.traverse((obj) => {
    if (!obj.isMesh) return;
    const match = obj.userData?.barId === barId;
    if (obj.material && obj.material.emissive) {
      obj.material.emissive.setHex(match ? 0xffd54f : 0x000000);
    }
  });
}

function computeEvidenceBounds() {
  evidenceBounds = null;
  if (!currentModel) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const c of Object.values(currentModel.components || {})) {
    if (c.kind !== 'tower_bar') continue;
    const p = c.properties || {};
    for (const k of ['x1_px', 'x2_px']) {
      if (typeof p[k] === 'number') { minX = Math.min(minX, p[k]); maxX = Math.max(maxX, p[k]); }
    }
    for (const k of ['y1_px', 'y2_px']) {
      if (typeof p[k] === 'number') { minY = Math.min(minY, p[k]); maxY = Math.max(maxY, p[k]); }
    }
  }
  if ([minX, minY, maxX, maxY].some((v) => !isFinite(v))) return;
  evidenceBounds = { minX, minY, maxX, maxY };
}

function toCanvas(x, y) {
  const cv = $('evidence-canvas');
  const W = cv.width, H = cv.height;
  if (evidenceImg) {
    const sw = evidenceImg.naturalWidth, sh = evidenceImg.naturalHeight;
    const scale = Math.min(W / sw, H / sh);
    const dw = sw * scale, dh = sh * scale;
    const ox = (W - dw) / 2, oy = (H - dh) / 2;
    return [x * scale + ox, y * scale + oy];
  }
  if (!evidenceBounds) return [x, y];
  const { minX, minY, maxX, maxY } = evidenceBounds;
  const w = (maxX - minX) || 1, h = (maxY - minY) || 1;
  const pad = 0.08;
  const scale = Math.min((W * (1 - 2 * pad)) / w, (H * (1 - 2 * pad)) / h);
  const ox = (W - w * scale) / 2, oy = (H - h * scale) / 2;
  return [(x - minX) * scale + ox, (y - minY) * scale + oy];
}

function redrawEvidence() {
  const cv = $('evidence-canvas');
  const ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = '#0e1116';
  ctx.fillRect(0, 0, cv.width, cv.height);
  if (evidenceImg) {
    const sw = evidenceImg.naturalWidth, sh = evidenceImg.naturalHeight;
    const scale = Math.min(cv.width / sw, cv.height / sh);
    const dw = sw * scale, dh = sh * scale;
    ctx.drawImage(evidenceImg, (cv.width - dw) / 2, (cv.height - dh) / 2, dw, dh);
  }
  if (!currentModel) return;
  const bars = Object.values(currentModel.components || {}).filter((c) => c.kind === 'tower_bar');
  for (const c of bars) {
    const p = c.properties || {};
    const vals = [p.x1_px, p.y1_px, p.x2_px, p.y2_px];
    if (vals.some((v) => typeof v !== 'number')) continue;
    const [ax, ay] = toCanvas(p.x1_px, p.y1_px);
    const [bx, by] = toCanvas(p.x2_px, p.y2_px);
    const isHl = c.id === highlightedBarId;
    ctx.strokeStyle = isHl ? '#ffd54f' : 'rgba(120,170,220,0.55)';
    ctx.lineWidth = isHl ? 3 : 1;
    ctx.beginPath();
    ctx.moveTo(ax, ay);
    ctx.lineTo(bx, by);
    ctx.stroke();
    if (isHl) {
      ctx.fillStyle = '#ffd54f';
      [ [ax, ay], [bx, by] ].forEach(([px, py]) => {
        ctx.beginPath(); ctx.arc(px, py, 4, 0, Math.PI * 2); ctx.fill();
      });
    }
  }
}

function highlightBar2D(id, comp) {
  highlightedBarId = id;
  const p = comp.properties || {};
  const hasCoords = ['x1_px', 'y1_px', 'x2_px', 'y2_px'].every((k) => typeof p[k] === 'number');
  document.querySelectorAll('.bar-item').forEach((el) => {
    el.classList.toggle('bar-active', el.dataset.barId === id);
  });
  if (hasCoords) {
    redrawEvidence();
    $('evidence-status').textContent = `已高亮：${id}（${p.x1_px},${p.y1_px} → ${p.x2_px},${p.y2_px}）`;
  } else {
    redrawEvidence();
    $('evidence-status').textContent = `${id} 无像素坐标（可能是 DXF 矢量杆件）`;
  }
}

function updateConfirmButton() {
  const btn = $('confirm-scan');
  if (!btn || !currentModel) return;
  const pending = Object.values(currentModel.components).some(
    (c) => (c.kind === 'tower_bar' || c.kind === 'tower_node') &&
      c.properties?.solve_status === 'pending_review',
  );
  btn.disabled = !pending || !currentModelPath;
}

async function loadAuditLog() {
  try {
    const res = await fetch('/api/audit');
    const data = await res.json();
    const box = $('audit-log');
    if (!box) return;
    box.innerHTML = (data.entries || []).slice(-20).reverse().map((e) =>
      `<div class="audit-row"><span class="audit-ts">${e.ts}</span> ` +
      `<b>${e.event}</b> ${JSON.stringify({ ...e, ts: undefined, event: undefined })}</div>`,
    ).join('') || '尚无审计记录';
  } catch (e) {
    $('audit-log').textContent = '审计日志加载失败';
  }
}

async function confirmDerivedY() {
  if (!currentModelPath) return;
  const btn = $('confirm-derived-y');
  btn.disabled = true;
  try {
    const res = await fetch('/api/confirm-derived-y', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_path: currentModelPath }),
    });
    const payload = await res.json();
    if (payload.ok) {
      $('status').textContent = `插值 Y 复核 ✓ confirmed=${payload.confirmed_nodes || 0}`;
      await renderBars(currentModelPath);
      await loadAuditLog();
    } else {
      $('status').textContent = '插值 Y 复核失败：' + (payload.error || '未知');
    }
  } catch (e) {
    $('status').textContent = '插值 Y 复核请求失败：' + e;
  } finally {
    btn.disabled = false;
    updateDerivedYButton();
  }
}

async function confirmScan() {
  if (!currentModelPath) return;
  const btn = $('confirm-scan');
  btn.disabled = true;
  try {
    const res = await fetch('/api/confirm-scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_path: currentModelPath }),
    });
    const payload = await res.json();
    if (payload.ok) {
      $('status').textContent = `扫描确认 ✓ verified=${payload.verified_components}`;
      await renderBars(currentModelPath);
      await loadAuditLog();
    } else {
      $('status').textContent = '确认失败：' + (payload.error || '未知');
    }
  } catch (e) {
    $('status').textContent = '确认请求失败：' + e;
  } finally {
    btn.disabled = false;
    updateConfirmButton();
  }
}

async function run() {
  const fileInput = $('file');
  if (!fileInput.files.length) {
    $('status').textContent = '请先选择图纸文件';
    return;
  }
  const file = fileInput.files[0];
  const dataB64 = await new Promise((resolve) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',')[1]);
    r.readAsDataURL(file);
  });
  const options = { merge: $('merge').checked };
  const bomInput = $('bom');
  if (bomInput.files.length) {
    const bomB64 = await new Promise((resolve) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(',')[1]);
      r.readAsDataURL(bomInput.files[0]);
    });
    options.bom_b64 = bomB64;
    options.bom_name = bomInput.files[0].name;
  }
  const btn = $('run');
  btn.disabled = true;
  $('status').textContent = '运行中…（DXF 全链约 10~30s）';
  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: file.name, data_b64: dataB64, options }),
    });
    const payload = await res.json();
    if (payload.ok !== false) {
      $('status').textContent = '完成 ✓\n' + Object.entries(payload).filter(([k, v]) => v && k !== 'steps').map(([k, v]) => `${k}=${v}`).join('\n');
      renderSteps((payload.steps && payload.steps.steps) || []);
      if (payload.glb_path) loadGlb(payload.glb_path);
      if (payload.model_path) renderBars(payload.model_path);
      await loadAuditLog();
    } else {
      $('status').textContent = '失败 ✗\n' + (payload.error || '未知错误');
      renderSteps((payload.steps && payload.steps.steps) || []);
    }
  } catch (e) {
    $('status').textContent = '请求失败：' + e;
  } finally {
    btn.disabled = false;
  }
}

$('run').onclick = run;
$('confirm-scan').onclick = confirmScan;
$('confirm-derived-y').onclick = confirmDerivedY;
initViewer();
loadAuditLog();

$('source-image').onchange = (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  const url = URL.createObjectURL(f);
  const img = new Image();
  img.onload = () => { evidenceImg = img; redrawEvidence(); };
  img.src = url;
};
$('clear-2d').onclick = () => {
  highlightedBarId = null;
  document.querySelectorAll('.bar-item').forEach((el) => el.classList.remove('bar-active'));
  highlightBar3D(null);
  redrawEvidence();
  $('evidence-status').textContent = '已清空高亮。';
};
