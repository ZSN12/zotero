const $ = (id) => document.getElementById(id);
let scene, camera, renderer, controls;

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
  const bars = Object.entries(model.components || {}).filter(([, c]) => c.kind === 'tower_bar');
  const box = $('bars');
  box.innerHTML = '';
  bars.slice(0, 500).forEach(([id, c]) => {
    const div = document.createElement('div');
    div.className = 'bar-item';
    const src = c.source ? `${c.source.reference || ''} · ${c.source.detail || ''} · conf=${c.source.confidence || 0}` : '无来源';
    const bid = (c.properties || {}).bar_id || id;
    div.innerHTML = `<b>${bid}</b> <span class="src">${src}</span>`;
    div.onclick = () => {
      $('status').textContent = `追溯：${id}\n${src}\n${JSON.stringify(c.properties || {}, null, 2)}`;
    };
    box.appendChild(div);
  });
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
