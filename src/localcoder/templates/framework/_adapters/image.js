/* ── Image Adapter: Upload + Camera ── */

let imageBase64 = null;

function getImageBase64() { return imageBase64; }
function clearImage() { imageBase64 = null; const p = document.getElementById('preview'); if(p) p.style.display='none'; }

function uploadImage() {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.onchange = e => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = ev => {
      imageBase64 = ev.target.result;
      const preview = document.getElementById('preview');
      if (preview) { preview.src = imageBase64; preview.style.display = 'block'; }
    };
    reader.readAsDataURL(file);
  };
  input.click();
}

async function openCamera() {
  const video = document.getElementById('camVideo');
  const captureBtn = document.getElementById('captureBtn');
  if (!video) return;

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
    video.srcObject = stream;
    video.style.display = 'block';
    video.play();
    if (captureBtn) captureBtn.style.display = 'inline-flex';
  } catch (err) {
    alert('Camera not available: ' + err.message);
  }
}

function capturePhoto() {
  const video = document.getElementById('camVideo');
  if (!video || !video.srcObject) return;

  const canvas = document.createElement('canvas');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0);
  imageBase64 = canvas.toDataURL('image/jpeg', 0.85);

  // Show preview
  const preview = document.getElementById('preview');
  if (preview) { preview.src = imageBase64; preview.style.display = 'block'; }

  // Stop camera
  video.srcObject.getTracks().forEach(t => t.stop());
  video.style.display = 'none';
  const captureBtn = document.getElementById('captureBtn');
  if (captureBtn) captureBtn.style.display = 'none';
}
