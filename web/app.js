const $ = (id) => document.getElementById(id);
let scene, camera, renderer, controls;
let currentModel = null;       // 最近一次 renderBars 加载的 model.json
let evidenceImg = null;        // 源图铺底（可选）
let evidenceBounds = null;     // 所有杆件 x1/y1/x2/y2 的包围盒（用于缩放铺满）
let highlightedBarId = null;   // 当前高亮杆件

function initViewer() {
  try {
    const container = $('viewer');
    import('three').then((THREE) => {
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

function loadGlb(url) {
  if (!scene || !renderer) return;
  import('three/addons/loaders/GLTFLoader.js').then(({ GLTFLoader }) => {
    new GLTFLoader().load(url, (gltf) => {
      const old = scene.getObjectByName('tower');
      if (old) scene.remove(old);
      gltf.scene.name = 'tower';
      const box = new (window.THREE ? THREE.Box3 : null)?.setFromObject(gltf.scene);
      if (box) {
        const c = box.getCenter(new THREE.Vector3());
        const s = box.getSize(new THREE.Vector3()).length() / 2 || 1;
        camera.position.copy(c).add(new THREE.Vector3(s * 0.7, s * 0.8, s * 1.2));
        controls.target.copy(c);
      }
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
  const bars = Object.entries(model.components || {}).filter(([, c]) => c.kind === 'tower_bar');
  const box = $('bars');
  box.innerHTML = '';
  bars.slice(0, 500).forEach(([id, c]) => {
    const div = document.createElement('div');
    div.className = 'bar-item';
    div.dataset.barId = id;
    const src = c.source ? `${c.source.reference || ''} · ${c.source.detail || ''} · conf=${c.source.confidence || 0}` : '无来源';
    const bid = (c.properties || {}).bar_id || id;
    div.innerHTML = `<b>${bid}</b> <span class="src">${src}</span>`;
    div.onclick = () => {
      $('status').textContent = `追溯：${id}\n${src}\n${JSON.stringify(c.properties || {}, null, 2)}`;
      highlightBar2D(id, c);
    };
    box.appendChild(div);
  });
  // 依据杆件坐标刷新 2D 证据层的坐标范围（即使未选源图）
  computeEvidenceBounds();
}

// ---------- 2D 证据层（P2-10，只读双向高亮） ----------

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
    // 有源图：按源图像素坐标 1:1（源图被缩放到画布尺寸）
    const sw = evidenceImg.naturalWidth, sh = evidenceImg.naturalHeight;
    const scale = Math.min(W / sw, H / sh);
    return [x * scale, y * scale];
  }
  // 无源图：按杆件坐标包围盒铺满画布（留 8% 边距）
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

  // 铺底源图
  if (evidenceImg) {
    const sw = evidenceImg.naturalWidth, sh = evidenceImg.naturalHeight;
    const scale = Math.min(cv.width / sw, cv.height / sh);
    const dw = sw * scale, dh = sh * scale;
    ctx.drawImage(evidenceImg, (cv.width - dw) / 2, (cv.height - dh) / 2, dw, dh);
  }

  // 画所有带坐标的杆件（灰线），当前高亮杆件（亮色粗线）
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
  // 列表双向高亮
  document.querySelectorAll('.bar-item').forEach((el) => {
    el.classList.toggle('bar-active', el.dataset.barId === id);
  });
  if (hasCoords) {
    redrawEvidence();
    $('evidence-status').textContent = `已高亮：${id}（${p.x1_px},${p.y1_px} → ${p.x2_px},${p.y2_px}）`;
  } else {
    redrawEvidence();
    $('evidence-status').textContent = `${id} 无 x1_px/y1_px/x2_px/y2_px（可能是 DXF 矢量杆件，无像素坐标）`;
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
      const rules = payload.harness || payload;
      if (payload.glb_path) loadGlb(payload.glb_path);
      if (payload.model_path) renderBars(payload.model_path);
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
initViewer();

// 2D 证据层：源图铺底 + 清空高亮
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
  redrawEvidence();
  $('evidence-status').textContent = '已清空高亮。';
};
