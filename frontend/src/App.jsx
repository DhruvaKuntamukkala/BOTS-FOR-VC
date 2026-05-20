import React, { useState, useEffect } from 'react';
import axios from 'axios';

const API_URL = 'http://localhost:8000';

const PLATFORMS = [
  { match: 'meet.google.com',     name: 'Google Meet', color: '#34a853' },
  { match: 'zoom.us',             name: 'Zoom',        color: '#2d8cff' },
  { match: 'teams.microsoft.com', name: 'MS Teams',    color: '#5b5fc7' },
  { match: 'teams.live.com',      name: 'MS Teams',    color: '#5b5fc7' },
  { match: 'webex.com',           name: 'Webex',       color: '#00bceb' },
  { match: 'zoho.com',            name: 'Zoho',        color: '#e8261d' },
  { match: 'zohomeeting.com',     name: 'Zoho',        color: '#e8261d' },
];

function detectPlatform(url) {
  if (!url) return null;
  return PLATFORMS.find(p => url.includes(p.match)) || null;
}

const STEPS = ['Ready', 'Joining', 'Recording', 'Processing', 'Done'];

function getStepIndex(status) {
  if (!status || status === 'Waiting') return 0;
  if (status === 'Initializing' || status.includes('joining')) return 1;
  if (status === 'Recording...') return 2;
  if (status === 'Processing...') return 3;
  if (status === 'Completed') return 4;
  return 0;
}

export default function App() {
  const [url, setUrl] = useState('');
  const [sessionId, setSessionId] = useState(null);
  const [status, setStatus] = useState('Waiting');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [fromExtension, setFromExtension] = useState(false);

  const platform = detectPlatform(url);
  const stepIndex = getStepIndex(status);
  const isFailed = status.startsWith('Failed');
  const isCompleted = status === 'Completed';
  const isIdle = !sessionId || isCompleted || isFailed;

  // Pick up session linked from extension
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const sid = params.get('session_id');
    if (sid) {
      setSessionId(sid);
      setStatus('Bot joining meeting...');
      setFromExtension(true);
      window.history.replaceState({}, document.title, window.location.pathname);
    }
  }, []);

  // Poll status while session is active
  useEffect(() => {
    if (!sessionId || isCompleted || isFailed) return;
    const interval = setInterval(async () => {
      try {
        const res = await axios.get(`${API_URL}/status?session_id=${sessionId}`);
        const s = res.data.status;
        setStatus(s);
        if (s === 'Completed') {
          clearInterval(interval);
          const r = await axios.get(`${API_URL}/result?session_id=${sessionId}`);
          setResult(r.data);
        } else if (s.startsWith('Failed')) {
          clearInterval(interval);
        }
      } catch (_) {}
    }, 2000);
    return () => clearInterval(interval);
  }, [sessionId, isCompleted, isFailed]);

  const handleStart = async () => {
    if (!url.trim()) { setError('Please enter a meeting URL'); return; }
    setError(null);
    setLoading(true);
    setResult(null);
    setStatus('Initializing');
    try {
      const res = await axios.post(`${API_URL}/start-meeting`, { meeting_url: url.trim() });
      setSessionId(res.data.session_id);
      setStatus('Bot joining meeting...');
    } catch (e) {
      setError(e.response?.data?.detail || 'Failed to start session');
      setStatus('Waiting');
    } finally {
      setLoading(false);
    }
  };

  const handleStop = async () => {
    if (!sessionId) return;
    setLoading(true);
    try {
      await axios.post(`${API_URL}/stop-meeting`, { session_id: sessionId });
      setStatus('Processing...');
    } catch (e) {
      setError(e.response?.data?.detail || 'Failed to stop session');
    } finally {
      setLoading(false);
    }
  };

  const handleReset = () => {
    setUrl('');
    setSessionId(null);
    setStatus('Waiting');
    setResult(null);
    setError(null);
    setLoading(false);
    setFromExtension(false);
  };

  const formatDuration = (s) => {
    if (!s) return '0s';
    const m = Math.floor(s / 60), sec = s % 60;
    return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
  };

  const fillPct = `${(stepIndex / (STEPS.length - 1)) * 100}%`;

  return (
    <div className="root">
      <div className="grid-bg" />

      {/* Header */}
      <header className="header">
        <div className="brand">
          <span className="brand-logo">M</span>
          <span className="brand-name">indx</span>
        </div>
        <span className="brand-pill">Meeting Intelligence</span>
      </header>

      {/* Main */}
      <main className="main">
        <div className="card">

          {/* Title row */}
          <div className="card-title">
            <h2>Bot Control</h2>
            <p>Join any meeting automatically and capture audio + metadata</p>
          </div>

          <div className="divider" />

          {/* URL Input */}
          <div className="field">
            <label className="field-label">Meeting URL</label>
            <div className="input-wrap">
              <input
                className="url-input"
                type="url"
                placeholder="https://meet.google.com/abc-defg-hij"
                value={url}
                onChange={e => { setUrl(e.target.value); setError(null); }}
                disabled={!isIdle}
                onKeyDown={e => e.key === 'Enter' && isIdle && handleStart()}
              />
              {platform && (
                <span
                  className="platform-tag"
                  style={{ color: platform.color, background: platform.color + '18', borderColor: platform.color + '44' }}
                >
                  {platform.name}
                </span>
              )}
            </div>
            {fromExtension && <span className="hint">Session linked from browser extension</span>}
            {error && <span className="hint hint-error">{error}</span>}
          </div>

          {/* Action Buttons */}
          <div className="actions">
            {isIdle ? (
              <button className="btn btn-primary" onClick={handleStart} disabled={loading || !url.trim()}>
                {loading
                  ? <span className="spinner" />
                  : <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M3 2.5l10 5.5-10 5.5V2.5z"/></svg>
                }
                Launch Bot
              </button>
            ) : (
              <button className="btn btn-stop" onClick={handleStop} disabled={loading || status === 'Processing...'}>
                {loading
                  ? <span className="spinner" />
                  : <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor"><rect x="1" y="1" width="12" height="12" rx="2"/></svg>
                }
                Stop Recording
              </button>
            )}
            {(isCompleted || isFailed) && (
              <button className="btn btn-ghost" onClick={handleReset}>New Session</button>
            )}
          </div>

          {/* Progress Steps — shown once a session exists */}
          {sessionId && (
            <div className="progress-wrap">
              <div className="progress-track">
                <div className="progress-fill" style={{ width: fillPct }} />
              </div>
              <div className="steps">
                {STEPS.map((label, i) => {
                  const state = i < stepIndex ? 'done'
                    : i === stepIndex ? (isFailed ? 'failed' : 'active')
                    : 'pending';
                  return (
                    <div key={label} className={`step step-${state}`}>
                      <div className="step-circle">
                        {state === 'done'
                          ? <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="1,6 4.5,9.5 11,3"/></svg>
                          : state === 'active' && i === 2
                            ? <span className="rec-dot" />
                            : <span>{i + 1}</span>
                        }
                      </div>
                      <span className="step-label">{label}</span>
                    </div>
                  );
                })}
              </div>
              {isFailed && <p className="hint hint-error" style={{ marginTop: 8 }}>{status}</p>}
            </div>
          )}

          {/* Results */}
          {result && isCompleted && (
            <div className="result-card">
              <div className="result-heading">
                <span className="result-check">
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="1.5,7 5,10.5 12.5,3"/></svg>
                </span>
                Session complete
              </div>

              <div className="result-stats">
                <div className="stat">
                  <span className="stat-label">Duration</span>
                  <span className="stat-value">{formatDuration(result.duration)}</span>
                </div>
                <div className="stat">
                  <span className="stat-label">Participants</span>
                  <span className="stat-value">{result.participants?.length ?? 0}</span>
                </div>
              </div>

              {result.speaker_timeline?.length > 0 && (
                <div className="result-row">
                  <span className="result-row-label">Speaker Timeline</span>
                  <div className="timeline-list">
                    {result.speaker_timeline.map((seg, i) => (
                      <div key={i} className="timeline-item">
                        <div className="timeline-marker" />
                        <div className="timeline-content">
                          <span className="timeline-speaker">{seg.speaker}</span>
                          <span className="timeline-time">{seg.time_range ? seg.time_range.split('-').map(t => t + 's').join(' → ') : ''}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="result-row">
                <span className="result-row-label">Saved files</span>
                <div className="file-list">
                  <div className="file-entry">
                    <span className="file-icon">♪</span>
                    <span className="file-name">{result.audio_file?.split(/[\\/]/).pop()}</span>
                  </div>
                  <div className="file-entry">
                    <span className="file-icon">
                      <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor">
                        <path d="M4 1h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V3a2 2 0 0 1 2-2zm0 1a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V3a1 1 0 0 0-1-1H4z"/>
                        <path d="M4.5 4a.5.5 0 0 1 .5-.5h6a.5.5 0 0 1 0 1H5a.5.5 0 0 1-.5-.5zM4.5 7a.5.5 0 0 1 .5-.5h6a.5.5 0 0 1 0 1H5a.5.5 0 0 1-.5-.5zM4.5 10a.5.5 0 0 1 .5-.5h6a.5.5 0 0 1 0 1H5a.5.5 0 0 1-.5-.5z"/>
                      </svg>
                    </span>
                    <span className="file-name">{result.metadata_file?.split(/[\\/]/).pop()}</span>
                  </div>
                </div>
              </div>
            </div>
          )}

        </div>
      </main>
    </div>
  );
}
