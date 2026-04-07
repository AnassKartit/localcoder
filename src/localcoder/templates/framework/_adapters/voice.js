/* ── Voice Adapter: Record + Transcribe ── */

let mediaRecorder = null;
let audioChunks = [];
let audioBase64 = null;
let isRecording = false;

function getAudioBase64() { return audioBase64; }
function clearAudio() { audioBase64 = null; }

async function toggleRecording(btn) {
  if (isRecording) {
    stopRecording(btn);
  } else {
    await startRecording(btn);
  }
}

async function startRecording(btn) {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
    audioChunks = [];

    mediaRecorder.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data); };

    mediaRecorder.onstop = () => {
      const blob = new Blob(audioChunks, { type: 'audio/webm' });
      const reader = new FileReader();
      reader.onload = ev => {
        audioBase64 = ev.target.result;
        // Show audio player preview
        const player = document.getElementById('audioPreview');
        if (player) {
          player.src = URL.createObjectURL(blob);
          player.style.display = 'block';
        }
      };
      reader.readAsDataURL(blob);
      stream.getTracks().forEach(t => t.stop());
    };

    mediaRecorder.start();
    isRecording = true;
    if (btn) {
      btn.innerHTML = '<span class="btn-icon">⏹️</span> Stop';
      btn.classList.add('recording');
    }

    // Recording timer
    let seconds = 0;
    const timer = document.getElementById('recordTimer');
    const interval = setInterval(() => {
      seconds++;
      const m = Math.floor(seconds / 60).toString().padStart(2, '0');
      const s = (seconds % 60).toString().padStart(2, '0');
      if (timer) timer.textContent = `${m}:${s}`;
      if (!isRecording) clearInterval(interval);
    }, 1000);

  } catch (err) {
    alert('Microphone not available: ' + err.message);
  }
}

function stopRecording(btn) {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
  }
  isRecording = false;
  if (btn) {
    btn.innerHTML = '<span class="btn-icon">🎙️</span> Record';
    btn.classList.remove('recording');
  }
}
