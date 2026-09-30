// Track titles come from file metadata, so escape before building markup.
function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

// Shared live-stream tuning for both players.
//
// The live-sync distance is deliberately left at the hls.js default of three
// target durations. A segment only enters the playlist once it is completely
// written, so with 6s segments anything tighter leaves the player with no
// buffer: it finishes the newest segment and then waits for the next one to
// exist. That is a stall every few seconds. Playback-rate correction is left
// off too — chasing an unreachable sync point just makes the stream sound and
// look wrong. To cut latency, shorten the segments on the server rather than
// tighten the target here.
function buildHlsConfig() {
  return {
    enableWorker: true,
    backBufferLength: 10, // Reduce back buffer to 10s
    startPosition: -1, // Start at live edge
    liveDurationInfinity: true, // Treat as infinite live stream
    manifestLoadingTimeOut: 10000,
    manifestLoadingMaxRetry: 10,
    manifestLoadingRetryDelay: 1000,
    levelLoadingTimeOut: 10000,
    levelLoadingMaxRetry: 10,
    levelLoadingRetryDelay: 1000,
    fragLoadingTimeOut: 20000,
    fragLoadingMaxRetry: 10,
    fragLoadingRetryDelay: 1000,
    xhrSetup: function (xhr) {
      // Prevent the browser caching live playlists and segments
      xhr.setRequestHeader("Cache-Control", "no-cache, no-store");
    },
  };
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  return minutes > 0
    ? `${minutes}:${remainder.toString().padStart(2, "0")}`
    : `${remainder}s`;
}

class RadioPlayer {
  constructor() {
    this.audio = document.getElementById("radio-stream");
    this.playPauseBtn = document.getElementById("play-pause-btn");
    this.volumeSlider = document.getElementById("volume-slider");
    this.streamSource = document.getElementById("stream-source");
    this.video = document.getElementById("video-stream");
    this.videoStatus = document.getElementById("video-status");
    this.videoContainer = document.getElementById("video-player");
    this.audioPanel = document.getElementById("audio-player-panel");
    this.videoPanel = document.getElementById("video-player-panel");
    this.listenNavBtn = document.getElementById("nav-listen-btn");
    this.watchNavBtn = document.getElementById("nav-watch-btn");
    this.audioTimelineList = document.getElementById("timeline-list-audio");
    this.videoTimelineList = document.getElementById("timeline-list-video");

    this.isPlaying = false;
    this.isVideoPlaying = false;
    this.timelineRows = [];
    this.timelineClockOffset = 0;
    this.timelineTicker = null;
    this.renderedIndex = 0;
    this.config = null;
    this.currentShow = null;
    this.schedule = null;
    this.hasUserInteracted = false;
    this.videoPlaylistUrl = null;
    this.hlsInstance = null;
    this.audioHlsInstance = null;
    this.playbackMode = this.resolveInitialPlaybackMode();
    this.expectingVideoPause = false;
    this.videoAutoResumeAttempts = 0;
    this.videoAutoResumeTimer = null;
    this.videoHealthCheckInterval = null;
    this.lastVideoProgress = 0;
    this.videoStallCount = 0;
    this.videoRecoveryTimer = null;
    this.videoRecoveryAttempts = 0;
    this.videoRecoveryInFlight = false;
    this.videoRecoveryLastReason = null;
    this.videoRestartTimer = null;
    this.videoStartInFlight = false;
    this.videoLiveEdgeSeeked = false;
    this.audioLiveEdgeSeeked = false;
    this.videoAutoStartPending = false;
    this.videoAutoStartRequestedAuto = false;
    this.videoLastProgressAt = 0;
    this.videoLastPlaybackStartAt = 0;
    this.videoRestartGraceUntil = 0;
    this.connectionLostTimer = null;
    this.wasAudioPlaying = false;
    this.wasVideoPlaying = false;

    // Set better initial state
    this.setInitialTrackState();
    this.initializeVideoElement();
    this.initializePlaybackModeControls();

    this.updatePlaybackModeUrl(this.playbackMode);
    if (this.playbackMode === "video") {
      this.updateVideoStatus("Video ready");
    }

    window.addEventListener("popstate", () => this.syncPlaybackModeFromUrl());

    this.initializePlayer();
    this.initializeSocket();
    this.loadInitialData();

    // Start polling for track metadata
    this.startMetadataPolling();

    // Start video health monitoring
    this.startVideoHealthCheck();

    // Optional auto-start after everything is loaded
    this.attemptAutoStart();
  }

  initializePlayer() {
    // Set up event listeners
    this.playPauseBtn.addEventListener("click", () => {
      this.hasUserInteracted = true;
      this.togglePlayPause();
    });
    this.volumeSlider.addEventListener("input", (e) =>
      this.setVolume(e.target.value),
    );

    // Set initial volume
    this.audio.volume = this.volumeSlider.value / 100;

    // Optimize audio element for live streaming
    this.audio.preload = "none"; // Don't preload for live streams
    if (this.audio.mozPreservesPitch !== undefined) {
      this.audio.mozPreservesPitch = false; // Firefox optimization
    }
    if (this.audio.webkitPreservesPitch !== undefined) {
      this.audio.webkitPreservesPitch = false; // WebKit optimization
    }

    console.log("Audio element configured for livestream");

    // Audio event listeners with enhanced debugging
    this.audio.addEventListener("loadstart", () => {
      console.log("🔄 Audio connecting...");
      this.showLoadingState();
    });
    this.audio.addEventListener("loadeddata", () => {
      // Audio data loaded - no need to log
    });
    this.audio.addEventListener("canplay", () => {
      console.log("✅ Audio ready to play");
      this.hideLoadingState();
    });
    this.audio.addEventListener("canplaythrough", () => {
      // Audio can play through - no need to log
    });
    this.audio.addEventListener("error", (e) => {
      console.error("Audio error event:", e);
      console.error("Audio error details:", this.audio.error);
      this.handleAudioError(e);
    });
    this.audio.addEventListener("loadstart", () => {
      console.log("Audio loadstart - stream connecting");
      this.lastLoadStart = Date.now();
    });
    this.audio.addEventListener("play", () => {
      console.log("▶️ Playback started");
      this.updatePlayButton(true, "audio");
      if (!this.audioLiveEdgeSeeked) {
        this.audioLiveEdgeSeeked = true;
        setTimeout(() => this.seekAudioToLiveEdge(), 500);
      }
    });
    this.audio.addEventListener("pause", () => {
      console.log("⏸️ Playback paused");
      this.updatePlayButton(false, "audio");
    });
    this.audio.addEventListener("waiting", () => {
      console.log(
        "⏳ Buffering... Network state:",
        this.audio.networkState,
        "Ready state:",
        this.audio.readyState,
      );
      console.log("Audio error:", this.audio.error);
      console.log(
        "Buffered ranges:",
        this.audio.buffered.length > 0
          ? `${this.audio.buffered.start(0)}-${this.audio.buffered.end(0)}`
          : "none",
      );
      if (this.isPlaying) {
        document.getElementById("current-track").textContent = "Buffering...";
      }
    });
    this.audio.addEventListener("stalled", () => {
      console.log("Audio stalled");
      if (this.isPlaying) {
        console.log("Stream stalled - may need reconnection");
        // Don't immediately reconnect on stall, but start monitoring more closely
        setTimeout(() => {
          if (
            this.audio.networkState === HTMLMediaElement.NETWORK_NO_SOURCE &&
            this.isPlaying
          ) {
            console.log("Stream still stalled after timeout - reconnecting");
            this.handleAudioError(new Error("Stream stalled"));
          }
        }, 10000); // Wait 10 seconds before deciding it's truly stuck
      }
    });
    this.audio.addEventListener("suspend", () => {
      // Audio suspend is normal for live streams - browser managing buffer
    });
    this.audio.addEventListener("abort", () => {
      console.log("Audio aborted");
      if (this.isPlaying && !this.reconnectionTimer) {
        console.log("Stream aborted unexpectedly - reconnecting");
        this.handleAudioError(new Error("Stream aborted"));
      }
    });
    this.audio.addEventListener("emptied", () => {
      console.log("Audio emptied");
      if (this.isPlaying && !this.reconnectionTimer) {
        console.log("Stream emptied - reconnecting");
        this.handleAudioError(new Error("Stream emptied"));
      }
    });
  }

  initializeVideoElement() {
    if (!this.video) {
      return;
    }

    this.video.controls = true;
    this.video.preload = "none";
    this.video.defaultMuted = false;
    this.video.removeAttribute("muted");
    this.updateVideoStatus("Loading video stream...");

    this.video.addEventListener("play", (event) => {
      if (event?.isTrusted) {
        this.hasUserInteracted = true;
      }
      this.expectingVideoPause = false;
      this.videoAutoResumeAttempts = 0;
      this.videoLastPlaybackStartAt = Date.now();
      this.videoLastProgressAt = Date.now();
      this.videoRestartGraceUntil = Date.now() + 8000;
      this.isVideoPlaying = true;
      this.resetVideoRecovery("playback started");
      this.clearVideoAutoResumeTimer();
      this.updatePlayButton(true, "video");
      if (this.video.muted) {
        this.updateVideoStatus("Playing (muted)");
      } else {
        this.updateVideoStatus("Playing");
      }
      this.ensureVideoUnmuted();
    });
    this.video.addEventListener("pause", (event) => {
      const wasPlaying = this.isVideoPlaying;
      const userInitiatedPause = event.isTrusted || this.expectingVideoPause;

      this.updateVideoStatus("Paused");
      this.updatePlayButton(false, "video");
      this.expectingVideoPause = false;
      this.isVideoPlaying = false;

      if (this.playbackMode === "video" && wasPlaying && !userInitiatedPause) {
        this.scheduleVideoAutoResume();
      } else {
        this.clearVideoAutoResumeTimer();
        this.videoAutoResumeAttempts = 0;
      }
    });
    this.video.addEventListener("waiting", () => {
      this.updateVideoStatus("Buffering...");
    });
    this.video.addEventListener("canplay", () => {
      this.updateVideoStatus("Ready");
    });
    this.video.addEventListener("timeupdate", () => {
      this.videoLastProgressAt = Date.now();
    });
    this.video.addEventListener("error", (event) => {
      console.error("Video error event:", event);
      const mediaError = event?.currentTarget?.error;
      if (mediaError) {
        this.updateVideoStatus("Video playback error");
        if (mediaError.code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED) {
          this.queueVideoAutoStart(!this.hasUserInteracted);
          this.scheduleVideoRecovery("unsupported video source");
          return;
        }
      }
      this.scheduleVideoRecovery("video element error");
    });

    this.video.addEventListener("stalled", () => {
      console.warn("Video stalled");
      const now = Date.now();
      const lastProgressAge = this.videoLastProgressAt
        ? now - this.videoLastProgressAt
        : Number.POSITIVE_INFINITY;
      if (lastProgressAge > 15000) {
        this.updateVideoStatus("Video stalled - reconnecting...");
        this.scheduleVideoRecovery("video stalled");
      } else {
        this.updateVideoStatus("Buffering...");
      }
    });

    this.video.addEventListener("volumechange", () => {
      if (!this.video.muted) {
        if (!this.hasUserInteracted) {
          this.hasUserInteracted = true;
        }
        this.ensureVideoUnmuted();
        if (this.isVideoPlaying) {
          this.updateVideoStatus("Playing");
        }
      } else if (this.isVideoPlaying) {
        this.updateVideoStatus("Playing (muted)");
      }
    });
  }

  initializePlaybackModeControls() {
    if (this.listenNavBtn) {
      this.listenNavBtn.addEventListener("click", () => {
        this.hasUserInteracted = true;
        this.setPlaybackMode("audio");
      });
    }

    if (this.watchNavBtn) {
      this.watchNavBtn.addEventListener("click", () => {
        this.hasUserInteracted = true;
        this.setPlaybackMode("video");
      });
    }

    this.updatePlaybackModeUI();
  }

  setPlaybackMode(mode) {
    if (!mode) {
      return;
    }

    const normalizedMode = mode === "video" ? "video" : "audio";

    this.updatePlaybackModeUrl(normalizedMode);

    if (normalizedMode === this.playbackMode) {
      return;
    }

    if (normalizedMode === "video") {
      this.pauseAudioPlayback({ updateButton: false });
      this.playbackMode = "video";
      this.updatePlaybackModeUI();
      this.updatePlayButton(this.isVideoPlaying, "video");
      this.updateVideoConfig();
      this.startVideoPlayback();
    } else {
      this.pauseVideoPlayback({ updateButton: false });
      this.playbackMode = "audio";
      this.updatePlaybackModeUI();
      this.updateVideoStatus("Video ready");
      this.updatePlayButton(this.isPlaying, "audio");
      this.clearVideoRecoveryTimer();
    }
  }

  updatePlaybackModeUI() {
    if (this.listenNavBtn) {
      this.listenNavBtn.classList.toggle(
        "active",
        this.playbackMode === "audio",
      );
    }
    if (this.watchNavBtn) {
      this.watchNavBtn.classList.toggle(
        "active",
        this.playbackMode === "video",
      );
    }
    if (this.audioPanel) {
      this.audioPanel.classList.toggle("hidden", this.playbackMode !== "audio");
    }
    if (this.videoPanel) {
      this.videoPanel.classList.toggle("hidden", this.playbackMode !== "video");
    }
    if (this.videoContainer) {
      this.videoContainer.classList.toggle(
        "active",
        this.playbackMode === "video",
      );
    }

    if (this.playPauseBtn) {
      if (this.playbackMode === "video") {
        this.playPauseBtn.setAttribute(
          "aria-label",
          "Play or pause video stream",
        );
      } else {
        this.playPauseBtn.setAttribute(
          "aria-label",
          "Play or pause audio stream",
        );
      }
    }
  }

  resolveInitialPlaybackMode() {
    try {
      const url = new URL(window.location.href);
      const modeParam = url.searchParams.get("mode");
      if (modeParam) {
        const normalized = modeParam.toLowerCase();
        if (normalized === "video" || normalized === "audio") {
          return normalized;
        }
      }

      const hash = (url.hash || "").replace("#", "").toLowerCase();
      if (hash === "watch" || hash === "video") {
        return "video";
      }
    } catch (error) {
      console.warn("Unable to resolve playback mode from URL:", error);
    }

    return "audio";
  }

  updatePlaybackModeUrl(mode) {
    try {
      const url = new URL(window.location.href);

      if (mode === "video") {
        url.searchParams.set("mode", "video");
      } else {
        url.searchParams.delete("mode");
      }

      const nextPath = `${url.pathname}${url.search}${url.hash}`;
      if (
        nextPath !==
        window.location.pathname + window.location.search + window.location.hash
      ) {
        window.history.replaceState({}, "", nextPath);
      }
    } catch (error) {
      console.warn("Failed to update playback mode URL:", error);
    }
  }

  syncPlaybackModeFromUrl() {
    const modeFromUrl = this.resolveInitialPlaybackMode();
    if (modeFromUrl !== this.playbackMode) {
      this.setPlaybackMode(modeFromUrl);
    }
  }

  startVideoPlayback(options = {}) {
    if (!this.video || this.playbackMode !== "video") {
      return;
    }

    if (this.videoStartInFlight) {
      return;
    }

    if (this.isVideoPlaying && !this.video.paused) {
      this.ensureVideoUnmuted();
      this.updatePlayButton(true, "video");
      return;
    }

    const autoStart = options.auto === true;

    this.videoStartInFlight = true;
    this.pauseAudioPlayback({ updateButton: false });
    this.videoRestartGraceUntil = Date.now() + 8000;

    if (!this.hlsInstance && !this.video.src && !this.video.currentSrc) {
      this.queueVideoAutoStart(autoStart);
      this.videoStartInFlight = false;
      this.updateVideoStatus("Waiting for video stream...");
      this.scheduleVideoRecovery("missing video source");
      return;
    }

    this.expectingVideoPause = false;
    this.clearVideoAutoResumeTimer();
    this.videoAutoResumeAttempts = 0;

    const shouldStartMuted = autoStart && !this.hasUserInteracted;

    if (shouldStartMuted) {
      this.video.muted = true;
      this.video.defaultMuted = true;
      this.video.setAttribute("muted", "");
    } else {
      this.ensureVideoUnmuted();
    }

    this.updateVideoStatus("Connecting to video stream...");

    const playPromise = this.video.play();

    const handleSuccess = () => {
      this.isVideoPlaying = true;
      this.videoLastPlaybackStartAt = Date.now();
      this.videoLastProgressAt = Date.now();
      this.updatePlayButton(true, "video");
      if (shouldStartMuted && this.video.muted) {
        this.updateVideoStatus("Playing (muted)");
      } else {
        this.ensureVideoUnmuted();
        this.updateVideoStatus("Playing");
      }
      if (!this.videoLiveEdgeSeeked) {
        this.videoLiveEdgeSeeked = true;
        setTimeout(() => this.seekVideoToLiveEdge(), 500);
      }
    };

    const handleFailure = (error) => {
      if (error) {
        console.error("Video playback error:", error);
      }
      this.isVideoPlaying = false;
      this.updatePlayButton(false, "video");
      if (shouldStartMuted) {
        this.updateVideoStatus("Tap play to start video");
      } else {
        this.updateVideoStatus("Tap play to start video");
      }
      if (error && error.name === "NotAllowedError") {
        return;
      }
      if (error && error.name === "NotSupportedError") {
        this.queueVideoAutoStart(autoStart);
        this.updateVideoStatus("Waiting for video stream...");
        this.scheduleVideoRecovery("unsupported video source");
        return;
      }
      this.scheduleVideoRecovery("playback start failed");
    };

    if (playPromise !== undefined) {
      playPromise
        .then(handleSuccess)
        .catch(handleFailure)
        .finally(() => {
          this.videoStartInFlight = false;
        });
    } else {
      handleSuccess();
      this.videoStartInFlight = false;
    }
  }

  pauseVideoPlayback({ updateButton = true } = {}) {
    if (!this.video) {
      return;
    }

    this.expectingVideoPause = true;
    this.clearVideoAutoResumeTimer();
    this.videoAutoResumeAttempts = 0;
    this.resetVideoRecovery("video paused");

    if (!this.video.paused) {
      this.video.pause();
    }

    this.isVideoPlaying = false;

    if (updateButton) {
      this.updatePlayButton(false, "video");
    }

    if (this.playbackMode === "video") {
      this.updateVideoStatus("Paused");
    } else {
      this.updateVideoStatus("Video ready");
    }
  }

  clearVideoAutoResumeTimer() {
    if (this.videoAutoResumeTimer) {
      clearTimeout(this.videoAutoResumeTimer);
      this.videoAutoResumeTimer = null;
    }
  }

  scheduleVideoAutoResume() {
    if (!this.video || this.playbackMode !== "video") {
      return;
    }

    // Remove the 3-attempt limit - keep trying to reconnect
    this.videoAutoResumeAttempts += 1;
    this.clearVideoAutoResumeTimer();

    console.warn(
      `Video paused unexpectedly - retrying playback (attempt ${this.videoAutoResumeAttempts})`,
    );

    this.updateVideoStatus("Reconnecting video stream...");

    // Exponential backoff with max of 5 seconds
    const delay = Math.min(
      1000 * Math.pow(1.5, Math.min(this.videoAutoResumeAttempts - 1, 5)),
      5000,
    );

    this.videoAutoResumeTimer = setTimeout(() => {
      this.videoAutoResumeTimer = null;

      if (
        this.playbackMode !== "video" ||
        this.expectingVideoPause ||
        !this.video.paused
      ) {
        return;
      }

      this.startVideoPlayback();
    }, delay);
  }

  clearVideoRecoveryTimer() {
    if (this.videoRecoveryTimer) {
      clearTimeout(this.videoRecoveryTimer);
      this.videoRecoveryTimer = null;
    }
  }

  resetVideoRecovery(reason = "") {
    if (reason) {
      console.log(`Video recovery reset: ${reason}`);
    }
    this.clearVideoRecoveryTimer();
    if (this.videoRestartTimer) {
      clearTimeout(this.videoRestartTimer);
      this.videoRestartTimer = null;
    }
    this.videoRecoveryAttempts = 0;
    this.videoRecoveryInFlight = false;
    this.videoRecoveryLastReason = null;
  }

  scheduleVideoRecovery(reason = "unknown", { immediate = false } = {}) {
    if (!this.video || this.playbackMode !== "video") {
      return;
    }
    if (this.expectingVideoPause) {
      return;
    }
    if (this.videoRecoveryTimer || this.videoRecoveryInFlight) {
      return;
    }
    if (this.isVideoPlaying && this.video.paused) {
      return;
    }
    if (this.videoAutoStartPending) {
      return;
    }
    if (
      this.videoRestartGraceUntil &&
      Date.now() < this.videoRestartGraceUntil
    ) {
      return;
    }

    this.videoRecoveryAttempts += 1;
    this.videoRecoveryLastReason = reason;

    const backoff = Math.min(
      1000 * Math.pow(1.6, Math.min(this.videoRecoveryAttempts - 1, 6)),
      15000,
    );
    const delay = immediate ? 0 : backoff;

    console.warn(
      `Scheduling video recovery (${reason}) in ${delay}ms (attempt ${this.videoRecoveryAttempts})`,
    );
    this.updateVideoStatus("Reconnecting video stream...");

    this.videoRecoveryTimer = setTimeout(() => {
      this.videoRecoveryTimer = null;
      this.attemptVideoRecovery();
    }, delay);
  }

  async attemptVideoRecovery() {
    if (this.videoRecoveryInFlight) {
      return;
    }
    if (!this.video || this.playbackMode !== "video") {
      return;
    }

    this.videoRecoveryInFlight = true;

    try {
      const playlist =
        this.videoPlaylistUrl || this.resolveVideoPlaylist() || null;
      if (!playlist) {
        this.updateVideoStatus("Video stream unavailable");
        this.scheduleVideoRecovery("missing playlist");
        return;
      }

      const available = await this.isVideoPlaylistAvailable(playlist);
      if (!available) {
        this.updateVideoStatus("Waiting for video stream...");
        this.scheduleVideoRecovery("playlist unavailable");
        return;
      }

      this.updateVideoStatus("Reconnecting video stream...");
      this.setupVideoStream(playlist, { force: true });

      const shouldAutoStart = this.playbackMode === "video";
      if (shouldAutoStart) {
        this.queueVideoAutoStart(
          !this.hasUserInteracted && !this.isVideoPlaying,
        );
      }
    } finally {
      this.videoRecoveryInFlight = false;
    }
  }

  async isVideoPlaylistAvailable(playlistUrl) {
    const cacheBustedUrl = playlistUrl.includes("?")
      ? `${playlistUrl}&t=${Date.now()}`
      : `${playlistUrl}?t=${Date.now()}`;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);

    try {
      const response = await fetch(cacheBustedUrl, {
        cache: "no-store",
        signal: controller.signal,
      });
      return response.ok;
    } catch (error) {
      return false;
    } finally {
      clearTimeout(timeout);
    }
  }

  pauseAudioPlayback({ updateButton = true } = {}) {
    if (!this.audio) {
      return;
    }

    if (this.reconnectionTimer) {
      clearTimeout(this.reconnectionTimer);
      this.reconnectionTimer = null;
    }

    if (!this.audio.paused) {
      this.audio.pause();
    }

    this.isPlaying = false;

    if (updateButton) {
      this.updatePlayButton(false, "audio");
    }
  }

  updateVideoStatus(message) {
    if (this.videoStatus) {
      this.videoStatus.textContent = message;
    }
  }

  ensureVideoUnmuted() {
    if (!this.video) {
      return;
    }

    if (this.hasUserInteracted) {
      this.video.muted = false;
      this.video.defaultMuted = false;
      this.video.removeAttribute("muted");
    }
  }

  resolveVideoPlaylist() {
    if (!this.config) {
      return null;
    }

    if (this.config.video_stream_url) {
      return this.config.video_stream_url;
    }

    if (this.config.video_stream?.playlist) {
      return this.config.video_stream.playlist;
    }

    if (this.config.station?.video_stream?.playlist) {
      return this.config.station.video_stream.playlist;
    }

    return null;
  }

  updateVideoConfig({ force = false } = {}) {
    const playlist = this.resolveVideoPlaylist();
    if (!playlist) {
      this.updateVideoStatus("Video stream unavailable");
      this.isVideoPlaying = false;
      if (this.playbackMode === "video") {
        this.updatePlayButton(false, "video");
      }
      return;
    }

    if (!force && this.videoPlaylistUrl === playlist && this.hlsInstance) {
      return;
    }

    this.setupVideoStream(playlist, { force });
  }

  setupVideoStream(playlistUrl, { force = false } = {}) {
    if (!this.video) {
      return;
    }

    if (!force && this.videoPlaylistUrl === playlistUrl && this.hlsInstance) {
      return;
    }

    this.videoPlaylistUrl = playlistUrl;
    this.isVideoPlaying = false;
    this.videoLiveEdgeSeeked = false;
    this.videoRestartGraceUntil = Date.now() + 8000;
    if (this.playbackMode === "video") {
      this.updatePlayButton(false, "video");
    }
    const cacheBustedUrl = playlistUrl.includes("?")
      ? `${playlistUrl}&t=${Date.now()}`
      : `${playlistUrl}?t=${Date.now()}`;

    if (window.Hls && window.Hls.isSupported()) {
      if (this.hlsInstance) {
        this.hlsInstance.destroy();
      }

      this.hlsInstance = new window.Hls(buildHlsConfig());

      this.hlsInstance.on(window.Hls.Events.MEDIA_ATTACHED, () => {
        this.updateVideoStatus("Connecting to video stream...");
      });

      this.hlsInstance.on(window.Hls.Events.MANIFEST_PARSED, () => {
        this.updateVideoStatus("Ready");
        // Seek to live edge immediately when manifest is parsed
        if (!this.videoLiveEdgeSeeked) {
          this.videoLiveEdgeSeeked = true;
          this.seekVideoToLiveEdge();
        }
        this.maybeStartPendingVideo();
      });

      // Also seek to live edge when first fragment is loaded (backup)
      this.hlsInstance.on(window.Hls.Events.FRAG_LOADED, () => {
        if (!this.videoLiveEdgeSeeked) {
          this.videoLiveEdgeSeeked = true;
          this.seekVideoToLiveEdge();
        }
      });

      this.hlsInstance.on(window.Hls.Events.ERROR, (event, data) => {
        console.warn("HLS error:", data);
        if (data.fatal) {
          switch (data.type) {
            case window.Hls.ErrorTypes.NETWORK_ERROR:
              console.log("Fatal network error - attempting to recover");
              this.updateVideoStatus("Reconnecting video stream...");
              // Try to recover by restarting load
              setTimeout(() => {
                if (this.hlsInstance) {
                  this.hlsInstance.startLoad();
                }
              }, 1000);
              this.scheduleVideoRecovery("hls network error");
              break;
            case window.Hls.ErrorTypes.MEDIA_ERROR:
              console.log("Fatal media error - attempting to recover");
              this.updateVideoStatus("Recovering video stream...");
              this.hlsInstance.recoverMediaError();
              this.scheduleVideoRecovery("hls media error");
              break;
            default:
              console.error("Fatal error - recreating HLS instance");
              this.updateVideoStatus("Reconnecting video stream...");
              // Recreate the HLS instance after a delay
              setTimeout(() => {
                if (this.videoPlaylistUrl && this.playbackMode === "video") {
                  this.setupVideoStream(this.videoPlaylistUrl);
                  if (this.hasUserInteracted) {
                    this.startVideoPlayback();
                  }
                }
              }, 2000);
              this.scheduleVideoRecovery("hls fatal error");
              break;
          }
        } else {
          // Non-fatal error - just log it
          console.log("Non-fatal HLS error:", data.details);
        }
      });

      this.hlsInstance.loadSource(cacheBustedUrl);
      this.hlsInstance.attachMedia(this.video);
    } else if (this.video.canPlayType("application/vnd.apple.mpegurl")) {
      this.video.src = cacheBustedUrl;
      this.video.load();
      this.video.addEventListener(
        "loadedmetadata",
        () => {
          this.updateVideoStatus("Ready");
          if (!this.videoLiveEdgeSeeked) {
            this.videoLiveEdgeSeeked = true;
            this.seekVideoToLiveEdge();
          }
          this.maybeStartPendingVideo();
        },
        { once: true },
      );
    } else {
      this.updateVideoStatus("HLS playback not supported in this browser.");
      if (this.videoContainer) {
        this.videoContainer.classList.add("video-unsupported");
      }
    }
  }

  initializeSocket() {
    this.socket = io({
      reconnection: true,
      reconnectionAttempts: Infinity,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
      timeout: 20000,
    });

    // Track if we were previously connected (to detect reconnections)
    this.wasConnected = false;

    this.socket.on("connect", () => {
      console.log("Socket connected");
      this.clearConnectionLostOverlayTimer();
      if (this.wasConnected) {
        // This is a reconnection
        console.log("Reconnected to server");
        this.hideConnectionLostOverlay();
        this.loadInitialData();

        // Restart video stream if we were in video mode
        if (this.playbackMode === "video" && this.videoPlaylistUrl) {
          console.log("Restarting video stream after reconnection...");
          this.updateVideoStatus("Reconnecting video stream...");
          this.queueVideoRestart("socket reconnect", this.wasVideoPlaying);
        }
        this.restorePlaybackState("socket reconnect");
      }
      this.wasConnected = true;
    });

    this.socket.on("config", (data) => {
      this.config = data;
      this.updateStreamSource();
      this.updateStationInfo();
      this.updateVideoConfig();
      if (
        this.playbackMode === "video" &&
        !this.hasUserInteracted &&
        this.video &&
        (this.video.paused || !this.isVideoPlaying)
      ) {
        this.startVideoPlayback({ auto: true });
      }
      // Update schedule if it changed
      if (data.schedule && data.schedule.schedule) {
        this.schedule = data.schedule.schedule;
        this.renderSchedule(this.schedule);
        console.log("Schedule updated via Socket.IO");
      }
    });

    this.socket.on("current-show", (show) => {
      this.currentShow = show;
      this.updateCurrentShow();
    });

    this.socket.on("now-playing", (track) => {
      this.updateNowPlaying(track);
    });

    // Handle radio server availability status
    this.socket.on("server-status", (status) => {
      console.log(
        "Radio server status:",
        status.available ? "online" : "offline",
      );
      if (!status.available) {
        this.capturePlaybackState("server offline");
        this.scheduleConnectionLostOverlay("Radio server restarting...");
      } else {
        this.clearConnectionLostOverlayTimer();
        this.hideConnectionLostOverlay();
        // Reload data and restart streams
        this.loadInitialData();
        if (this.playbackMode === "video" && this.videoPlaylistUrl) {
          console.log("Restarting video stream after server recovery...");
          this.updateVideoStatus("Reconnecting video stream...");
          this.queueVideoRestart("server recovery", this.wasVideoPlaying);
        }
        this.restorePlaybackState("server recovery");
      }
    });

    this.socket.on("disconnect", (reason) => {
      console.log("Disconnected from server:", reason);
      this.capturePlaybackState("socket disconnect");
      this.updateVideoStatus(
        "Server disconnected - waiting for reconnection...",
      );
      this.scheduleConnectionLostOverlay();
    });

    // Handle connection errors
    this.socket.on("connect_error", (error) => {
      console.log("Connection error:", error.message);
      this.capturePlaybackState("socket error");
      this.updateVideoStatus("Connection error - retrying...");
      this.scheduleConnectionLostOverlay();
    });
  }

  capturePlaybackState(reason = "") {
    this.wasAudioPlaying = this.isPlaying;
    this.wasVideoPlaying = this.isVideoPlaying;
    console.log(
      `Captured playback state (${reason}): audio=${this.wasAudioPlaying}, video=${this.wasVideoPlaying}, mode=${this.playbackMode}`,
    );
  }

  restorePlaybackState(reason = "") {
    console.log(
      `Restoring playback state (${reason}): audio=${this.wasAudioPlaying}, video=${this.wasVideoPlaying}, mode=${this.playbackMode}`,
    );
    if (this.playbackMode === "video") {
      this.pauseAudioPlayback({ updateButton: false });
      if (this.wasVideoPlaying && !this.videoStartInFlight) {
        this.startVideoPlayback();
      }
    } else {
      this.pauseVideoPlayback({ updateButton: false });
      if (this.wasAudioPlaying && !this.isPlaying) {
        this.togglePlayPause();
      }
    }
  }

  scheduleConnectionLostOverlay(message = "Reconnecting to server...") {
    if (this.connectionLostTimer) {
      return;
    }
    this.connectionLostTimer = setTimeout(() => {
      this.connectionLostTimer = null;
      this.showConnectionLostOverlay(message);
    }, 3000);
  }

  clearConnectionLostOverlayTimer() {
    if (this.connectionLostTimer) {
      clearTimeout(this.connectionLostTimer);
      this.connectionLostTimer = null;
    }
  }

  showConnectionLostOverlay(message = "Reconnecting to server...") {
    // Create overlay if it doesn't exist
    let overlay = document.getElementById("connection-lost-overlay");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "connection-lost-overlay";
      overlay.innerHTML = `
        <div class="connection-lost-content">
          <i class="fas fa-plug"></i>
          <h3>Connection Lost</h3>
          <p class="connection-message">${message}</p>
          <div class="reconnect-spinner"></div>
        </div>
      `;
      overlay.style.cssText = `
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(0, 0, 0, 0.8);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 9999;
        color: white;
        text-align: center;
      `;
      const content = overlay.querySelector(".connection-lost-content");
      if (content) {
        content.style.cssText =
          "display: flex; flex-direction: column; align-items: center; gap: 1rem;";
      }
      const icon = overlay.querySelector("i");
      if (icon) {
        icon.style.cssText = "font-size: 3rem; color: #ff6b6b;";
      }
      const spinner = overlay.querySelector(".reconnect-spinner");
      if (spinner) {
        spinner.style.cssText = `
          width: 30px;
          height: 30px;
          border: 3px solid rgba(255,255,255,0.3);
          border-top-color: white;
          border-radius: 50%;
          animation: spin 1s linear infinite;
        `;
      }
      // Add spinner animation
      const style = document.createElement("style");
      style.textContent =
        "@keyframes spin { to { transform: rotate(360deg); } }";
      document.head.appendChild(style);
      document.body.appendChild(overlay);
    }
    overlay.style.display = "flex";
  }

  hideConnectionLostOverlay() {
    const overlay = document.getElementById("connection-lost-overlay");
    if (overlay) {
      overlay.style.display = "none";
    }
  }

  async loadInitialData() {
    try {
      console.log("Loading initial data...");

      // Load configuration
      const configResponse = await fetch("/api/config");
      if (!configResponse.ok) {
        throw new Error(`Config API returned ${configResponse.status}`);
      }

      this.config = await configResponse.json();
      console.log("Config loaded:", this.config);

      this.updateStreamSource();
      this.updateStationInfo();
      this.updateVideoConfig();
      if (
        this.playbackMode === "video" &&
        !this.hasUserInteracted &&
        this.video &&
        (this.video.paused || !this.isVideoPlaying)
      ) {
        this.startVideoPlayback({ auto: true });
      }

      // Load current show
      const showResponse = await fetch("/api/current-show");
      if (showResponse.ok) {
        this.currentShow = await showResponse.json();
        this.updateCurrentShow();
      }

      // Load schedule
      this.loadSchedule();

      // Load timeline
      this.loadTimeline();

      // Load initial weather and listener count
      this.updateWeather();
      this.updateListenerCount();
    } catch (error) {
      console.error("Error loading initial data:", error);
    }
  }

  updateStreamSource() {
    console.log("updateStreamSource called, config:", this.config);

    this.audioLiveEdgeSeeked = false;

    if (this.config && this.config.stream_url) {
      // Add cache-busting parameter for initial load
      const timestamp = Date.now();
      const cacheBustedUrl = `${this.config.stream_url}?t=${timestamp}`;
      const isHlsStream = cacheBustedUrl.includes(".m3u8");

      if (isHlsStream && window.Hls && window.Hls.isSupported()) {
        if (this.audioHlsInstance) {
          this.audioHlsInstance.destroy();
        }

        this.audioHlsInstance = new window.Hls(buildHlsConfig());

        this.audioLiveEdgeSeeked = false;

        this.audioHlsInstance.attachMedia(this.audio);
        this.audioHlsInstance.on(window.Hls.Events.MEDIA_ATTACHED, () => {
          this.audioHlsInstance.loadSource(cacheBustedUrl);
          this.audioHlsInstance.startLoad(-1);
          console.log("Audio HLS source attached:", cacheBustedUrl);
        });

        this.audioHlsInstance.on(window.Hls.Events.MANIFEST_PARSED, () => {
          // Seek to live edge immediately when manifest is parsed
          if (!this.audioLiveEdgeSeeked) {
            this.audioLiveEdgeSeeked = true;
            this.seekAudioToLiveEdge();
          }
        });

        this.audioHlsInstance.on(window.Hls.Events.ERROR, (event, data) => {
          console.warn("Audio HLS error:", data);
          if (data.fatal) {
            switch (data.type) {
              case window.Hls.ErrorTypes.NETWORK_ERROR:
                this.audioHlsInstance.startLoad();
                break;
              case window.Hls.ErrorTypes.MEDIA_ERROR:
                this.audioHlsInstance.recoverMediaError();
                break;
              default:
                this.audioHlsInstance.destroy();
            }
          }
        });
      } else {
        if (this.audioHlsInstance) {
          this.audioHlsInstance.destroy();
          this.audioHlsInstance = null;
        }

        // Set src directly on audio element instead of source element
        this.audio.src = cacheBustedUrl;
        console.log("Stream URL updated:", cacheBustedUrl);

        // Force the audio element to load the new source
        this.audio.load();
        this.audio.addEventListener(
          "loadedmetadata",
          () => this.seekAudioToLiveEdge(),
          { once: true },
        );
      }
    } else {
      console.error(
        "Cannot update stream source - config or stream_url missing:",
        {
          hasConfig: !!this.config,
          streamUrl: this.config?.stream_url,
        },
      );

      // Fallback to default stream URL
      const fallbackUrl = `/audio/live.m3u8?t=${Date.now()}`;
      this.audio.src = fallbackUrl;
      console.log("Using fallback stream URL:", fallbackUrl);
      this.audio.load();
      this.audio.addEventListener(
        "loadedmetadata",
        () => this.seekAudioToLiveEdge(),
        { once: true },
      );
    }
  }

  updateStationInfo() {
    if (this.config && this.config.station) {
      document.getElementById("station-name").textContent =
        this.config.station.station_name;
      document.title = `${this.config.station.station_name} - Live Radio`;
    }
  }

  updateCurrentShow() {
    if (!this.currentShow) return;

    const djName = this.formatDJName(this.currentShow.dj_name);
    const startTime = this.formatShowTime(this.currentShow.start_time);
    const endTime = this.formatShowTime(this.currentShow.end_time);
    const showTime = `${startTime} - ${endTime}`;

    document.getElementById("current-dj").textContent = djName;
    document.getElementById("show-time").textContent = showTime;

    // Create a description based on music folders
    const musicTypes = this.currentShow.music_folders.join(", ");
    document.getElementById("show-description").textContent =
      `Playing the best of ${musicTypes}`;

    // Re-render schedule to update the highlighted show
    if (this.schedule) {
      this.renderSchedule(this.schedule);
    }
  }

  updateNowPlaying(track) {
    if (!track) return;

    const live = this.pickHeardTrack(track);
    document.getElementById("current-track").textContent =
      live.title || "Unknown Title";
    document.getElementById("current-artist").textContent =
      live.artist || "Unknown Artist";
    this.updateAlbumArt(live.artwork_url);

    // Add fade-in animation
    const trackInfo = document.querySelector(".track-info");
    trackInfo.classList.remove("fade-in");
    setTimeout(() => trackInfo.classList.add("fade-in"), 10);
  }

  pickHeardTrack(track) {
    // The playlist carries every track change with its wall-clock start, so a
    // listener sitting behind the live edge can be shown the one they are
    // actually hearing rather than the one being encoded.
    if (!track.events || track.events.length === 0) {
      return track;
    }

    const streamNow = this.streamNow();
    let heard = track.events[0];
    for (const event of track.events) {
      if (new Date(event.start).getTime() <= streamNow) {
        heard = event;
      }
    }
    return heard;
  }

  updateAlbumArt(artworkUrl) {
    const image = document.getElementById("album-art-image");
    const fallback = document.getElementById("album-art-fallback");
    if (!image || !artworkUrl || image.dataset.src === artworkUrl) {
      return;
    }

    // The URL carries a per-track cache buster, so only reload on a real change.
    image.dataset.src = artworkUrl;
    image.onload = () => {
      image.hidden = false;
      if (fallback) fallback.hidden = true;
    };
    image.onerror = () => {
      image.hidden = true;
      if (fallback) fallback.hidden = false;
    };
    image.src = artworkUrl;
  }

  async loadSchedule() {
    try {
      console.log("Loading schedule...");
      const response = await fetch("/api/schedule", { timeout: 10000 });

      if (!response.ok) {
        throw new Error(`Schedule API returned ${response.status}`);
      }

      const scheduleData = await response.json();

      if (!scheduleData || !scheduleData.schedule) {
        throw new Error("Invalid schedule data received");
      }

      this.schedule = scheduleData.schedule;
      this.renderSchedule(this.schedule);
      console.log(
        "Schedule loaded successfully:",
        this.schedule.length,
        "shows",
      );
    } catch (error) {
      console.error("Error loading schedule:", error);

      // Show error message in the schedule list
      const scheduleList = document.getElementById("schedule-list");
      scheduleList.innerHTML = `
        <div class="error-message" style="padding: 1rem; text-align: center; color: #ff6b6b;">
          <p>Unable to load schedule</p>
          <p style="font-size: 0.9rem; opacity: 0.8;">Retrying...</p>
        </div>
      `;

      // Retry after a short delay
      setTimeout(() => this.loadSchedule(), 5000);
    }
  }

  renderSchedule(schedule) {
    const scheduleList = document.getElementById("schedule-list");
    scheduleList.innerHTML = "";

    schedule.forEach((show) => {
      const item = document.createElement("div");
      item.className = "schedule-item";

      // Check if this is the current show by comparing with currentShow
      if (this.currentShow && show.dj_name === this.currentShow.dj_name) {
        item.classList.add("current");
      }

      const djName = this.formatDJName(show.dj_name);

      // Format times in local timezone
      const startTime = this.formatScheduleTime(show.start_time);
      const endTime = this.formatScheduleTime(show.end_time);
      const timeRange = `${startTime} - ${endTime}`;

      item.innerHTML = `
                <div class="schedule-time">${timeRange}</div>
                <div class="schedule-dj">${djName}</div>
            `;

      scheduleList.appendChild(item);
    });
  }

  formatScheduleTime(timeString) {
    // Parse the schedule time and format it for local timezone display
    const [hours, minutes] = timeString.split(":");
    const today = new Date();
    const scheduleTime = new Date(
      today.getFullYear(),
      today.getMonth(),
      today.getDate(),
      parseInt(hours),
      parseInt(minutes),
    );

    return scheduleTime.toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "Europe/London",
    });
  }

  formatShowTime(timeValue) {
    if (!timeValue) {
      return "";
    }

    if (typeof timeValue === "string") {
      const trimmed = timeValue.trim();
      if (/^\d{1,2}:\d{2}$/.test(trimmed)) {
        const normalized = trimmed.length === 4 ? `0${trimmed}` : trimmed;
        return this.formatScheduleTime(normalized);
      }
    }

    const date = new Date(timeValue);
    if (!Number.isNaN(date.getTime())) {
      return date.toLocaleTimeString("en-GB", {
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "Europe/London",
      });
    }

    return String(timeValue);
  }

  async updateWeather() {
    try {
      // We'll need to add a weather endpoint to the server
      const response = await fetch("/api/weather");
      if (response.ok) {
        const weather = await response.json();
        const weatherText = document.getElementById("weather-text");
        if (weatherText && weather) {
          weatherText.textContent = `${weather.condition}, ${weather.temperature}°C`;
        }
      }
    } catch (error) {
      console.error("Error updating weather:", error);
    }
  }

  async updateListenerCount() {
    try {
      const response = await fetch("/api/now-playing");
      if (response.ok) {
        const data = await response.json();
        const listenerCount = document.getElementById("listener-count");
        if (listenerCount) {
          listenerCount.textContent = `Listeners: ${data.listeners || 0}`;
        }
        // Same payload as the metadata poll. Pick the track the listener is
        // actually hearing (not the encoder edge) so the art matches the title;
        // pickHeardTrack falls back to the top-level track before playback starts.
        this.updateAlbumArt(this.pickHeardTrack(data).artwork_url);
      }
    } catch (error) {
      console.error("Error updating listener count:", error);
    }
  }

  async loadTimeline() {
    try {
      const response = await fetch("/api/timeline/upcoming?count=10");
      if (!response.ok) {
        console.warn("Timeline service not available");
        this.setTimelineMessage("Timeline unavailable", "error");
        return;
      }

      const timelineData = await response.json();
      // Progress and rollover are timed against the server's clock, not the
      // browser's, which may be off by any amount.
      this.timelineClockOffset = timelineData.current_time
        ? Date.now() - new Date(timelineData.current_time).getTime()
        : 0;
      this.timelineRows = timelineData.upcoming_items || [];
      this.renderTimeline();
      this.startTimelineTicker();
    } catch (error) {
      console.error("Error loading timeline:", error);
      this.setTimelineMessage("Timeline unavailable", "error");
    }
  }

  renderTimeline() {
    const lists = this.getTimelineLists();
    if (lists.length === 0) {
      return;
    }

    lists.forEach((list) => {
      list.innerHTML = "";
    });

    if (!this.timelineRows || this.timelineRows.length === 0) {
      this.setTimelineMessage("No upcoming items", "no-items");
      return;
    }

    this.renderedIndex = this.onAirIndex();
    const rows = this.timelineRows.slice(this.renderedIndex);

    rows.forEach((row, index) => {
      const onAir = index === 0;
      const classes = ["timeline-item", row.item_type];
      if (onAir) {
        classes.push("on-air");
      } else if (row.status === "preparing") {
        classes.push("preparing");
      }

      // Projected times roll forward from the audio actually on air; the
      // scheduled times drift because items are queued ahead of playback.
      const timeStr = onAir
        ? "NOW"
        : new Date(row.projected_start).toLocaleTimeString("en-GB", {
            hour: "2-digit",
            minute: "2-digit",
            timeZone: "Europe/London",
          });

      const title = escapeHtml(row.title);
      const description = row.artist
        ? `<strong>${escapeHtml(row.artist)}</strong> - ${title}`
        : title;

      const notes = [];
      if (row.style) {
        notes.push(`🎙️ ${escapeHtml(row.style.replace(/_/g, " "))}`);
      }
      if (row.dj_intro) {
        notes.push(`🎙️ DJ intro (${escapeHtml(row.dj_intro)})`);
      }
      if (row.dj_outro) {
        notes.push(`🎙️ DJ outro (${escapeHtml(row.dj_outro)})`);
      }
      const notesMarkup = notes.length
        ? `<div class="timeline-note">${notes.join(" · ")}</div>`
        : "";

      // The scheduler marks items "playing" as soon as it queues them, so its
      // status is meaningless here — only say what a listener can verify.
      const stateLabel = onAir
        ? "on air"
        : ["scheduled", "preparing"].includes(row.status)
          ? "preparing"
          : "";

      const progressMarkup = onAir
        ? `<div class="timeline-progress"><div class="timeline-progress-bar"></div></div>`
        : "";
      const durationMarkup = onAir
        ? `<span class="timeline-duration timeline-remaining"></span>`
        : `<span class="timeline-duration">${formatDuration(row.duration)}</span>`;

      const itemMarkup = `
        <div class="timeline-time">${timeStr}</div>
        <div class="timeline-content">
          <div class="timeline-description">${description}</div>
          ${notesMarkup}
          ${progressMarkup}
          <div class="timeline-meta">
            ${durationMarkup}
            ${
              stateLabel
                ? `<span class="timeline-status status-${stateLabel.replace(" ", "-")}">${stateLabel}</span>`
                : ""
            }
          </div>
        </div>
      `;

      lists.forEach((list) => {
        const timelineItem = document.createElement("div");
        timelineItem.className = classes.join(" ");
        timelineItem.innerHTML = itemMarkup;
        list.appendChild(timelineItem);
      });
    });

    this.updateOnAirProgress();
  }

  startTimelineTicker() {
    if (this.timelineTicker) {
      return;
    }
    this.timelineTicker = setInterval(() => this.tickTimeline(), 1000);
  }

  tickTimeline() {
    if (!this.timelineRows || this.timelineRows.length === 0) {
      return;
    }

    // Re-render only when the stream has moved into a different row, so the
    // page keeps pace with what is being heard instead of jumping every time
    // the poll comes back.
    if (this.onAirIndex() !== this.renderedIndex) {
      this.renderTimeline();
    } else {
      this.updateOnAirProgress();
    }
  }

  serverNow() {
    return Date.now() - (this.timelineClockOffset || 0);
  }

  streamNow() {
    // What the listener is hearing, on the server's clock. How far the player
    // sits behind the live edge is measured on the media timeline, because the
    // HLS program date-time cannot be trusted as wall clock: ffmpeg builds it
    // from the encoder's start time plus the duration it has encoded, so every
    // second the pipeline starves leaves the stamps permanently further behind
    // real time (the video pipeline runs hundreds of seconds behind after a
    // day). Both media elements expose seekable natively, so this works the
    // same under hls.js and Safari.
    const media = this.playbackMode === "video" ? this.video : this.audio;
    const edge = this.liveEdge(media);
    if (!Number.isNaN(edge)) {
      // ponytail: ignores the second or so between the producer writing audio
      // and the segment carrying it appearing in the playlist. Subtract a
      // server-reported publish lag if the progress bar ever needs to be
      // tighter than that.
      const behind = edge - media.currentTime;
      if (behind >= 0) {
        return this.serverNow() - behind * 1000;
      }
    }

    return this.serverNow();
  }

  liveEdge(media) {
    // End of the seekable range is the live edge of a live HLS stream. The
    // range set can be empty or in flux, and reading it then throws, so every
    // caller needs the same guards.
    if (!media || !media.seekable || media.seekable.length === 0) {
      return NaN;
    }
    try {
      const end = media.seekable.end(media.seekable.length - 1);
      return Number.isFinite(end) ? end : NaN;
    } catch (error) {
      console.log("Unable to read live edge:", error);
      return NaN;
    }
  }

  onAirIndex() {
    const streamNow = this.streamNow();
    const index = this.timelineRows.findIndex(
      (row) => this.rowEnd(row) > streamNow,
    );
    if (index !== -1) {
      return index;
    }
    // Nothing covers the playhead: fall back to what the server says is live.
    return Math.max(
      0,
      this.timelineRows.findIndex((row) => row.is_on_air),
    );
  }

  rowEnd(row) {
    return new Date(row.projected_end).getTime();
  }

  updateOnAirProgress() {
    const row = this.timelineRows && this.timelineRows[this.renderedIndex];
    if (!row) {
      return;
    }

    // The producer's end time is what counts: a song's tagged duration ignores
    // DJ talk mixed into the file and the overlap of a crossfade.
    const start = new Date(row.projected_start).getTime();
    const end = this.rowEnd(row);
    const span = end - start;
    if (!span) {
      return;
    }

    const elapsed = Math.min(Math.max(this.streamNow() - start, 0), span);
    document.querySelectorAll(".timeline-progress-bar").forEach((bar) => {
      bar.style.width = `${(elapsed / span) * 100}%`;
    });
    const remaining = formatDuration((span - elapsed) / 1000);
    document.querySelectorAll(".timeline-remaining").forEach((label) => {
      label.textContent = `${remaining} left`;
    });
  }

  getTimelineLists() {
    return [this.audioTimelineList, this.videoTimelineList].filter(Boolean);
  }

  setTimelineMessage(message, type = "error") {
    this.timelineRows = [];

    const className =
      type === "error" ? "error" : type === "no-items" ? "no-items" : type;

    this.getTimelineLists().forEach((list) => {
      list.innerHTML = `<div class="${className}">${message}</div>`;
    });
  }

  togglePlayPause() {
    if (this.playbackMode === "video") {
      if (this.isVideoPlaying) {
        this.pauseVideoPlayback();
      } else {
        this.startVideoPlayback();
      }
      return;
    }

    console.log("Toggle play/pause - current audio state:", this.isPlaying);
    console.log("Audio ready state:", this.audio.readyState);
    console.log("Audio network state:", this.audio.networkState);
    console.log("Audio src:", this.audio.src);

    if (this.isPlaying) {
      this.pauseAudioPlayback();

      // Stop any ongoing reconnection attempts
      if (this.reconnectionTimer) {
        clearTimeout(this.reconnectionTimer);
        this.reconnectionTimer = null;
      }
    } else {
      // Show connecting state
      document.getElementById("current-track").textContent = "Connecting...";
      document.getElementById("current-artist").textContent = "Starting stream";

      // Only force reload if audio source is not set or there's an error
      if (!this.audio.src || this.audio.error) {
        console.log("No source or error detected, reloading stream");
        this.forceStreamReload();
      }

      console.log("Attempting to play audio...");
      const playPromise = this.audio.play();

      if (playPromise !== undefined) {
        playPromise
          .then(() => {
            this.isPlaying = true;
            this.updatePlayButton(true, "audio");
            console.log("Audio playback started successfully");
          })
          .catch((error) => {
            console.error("Error playing audio:", error);
            console.error("Audio error code:", error.code);
            console.error("Audio error message:", error.message);
            this.handleAudioError(error);
          });
      } else {
        this.isPlaying = true;
        this.updatePlayButton(true, "audio");
      }
    }
  }

  forceStreamReload() {
    console.log("Reloading stream connection");

    this.audioLiveEdgeSeeked = false;

    // Pause but don't reset completely unless necessary
    this.audio.pause();

    // Only do a full reset if there's an actual error
    if (
      this.audio.error ||
      this.audio.networkState === HTMLMediaElement.NETWORK_NO_SOURCE
    ) {
      console.log("Full stream reset needed due to error state");
      this.audio.currentTime = 0;
      this.audio.src = "";
      this.audio.load();
    }

    // Use simpler cache-busting for local streams
    const timestamp = Date.now();
    const baseUrl = this.config?.stream_url || "/audio/live.m3u8";
    const freshUrl = `${baseUrl}?t=${timestamp}`;

    console.log("Loading stream URL:", freshUrl);
    if (this.audioHlsInstance) {
      this.audioHlsInstance.loadSource(freshUrl);
      this.audioHlsInstance.startLoad(-1);
    } else {
      this.audio.src = freshUrl;
      this.audio.load();
      this.audio.addEventListener(
        "loadedmetadata",
        () => this.seekAudioToLiveEdge(),
        { once: true },
      );
    }
  }

  seekAudioToLiveEdge() {
    // Try HLS.js live sync first (most reliable for live streams)
    if (
      this.audioHlsInstance &&
      typeof this.audioHlsInstance.liveSyncPosition === "number"
    ) {
      const livePos = this.audioHlsInstance.liveSyncPosition;
      if (Number.isFinite(livePos) && livePos > 0) {
        console.log(
          `Seeking audio to HLS live sync position: ${livePos.toFixed(1)}s`,
        );
        this.audio.currentTime = livePos;
        return;
      }
    }

    // Fallback to seekable range
    const end = this.liveEdge(this.audio);
    if (Number.isNaN(end)) {
      return;
    }
    // Seek very close to live edge
    const targetTime = Math.max(0, end - 0.5);
    console.log(
      `Seeking audio to live edge: ${targetTime.toFixed(1)}s (end: ${end.toFixed(1)}s)`,
    );
    this.audio.currentTime = targetTime;
  }

  seekVideoToLiveEdge() {
    // Try HLS.js live sync first (most reliable for live streams)
    if (
      this.hlsInstance &&
      typeof this.hlsInstance.liveSyncPosition === "number"
    ) {
      const livePos = this.hlsInstance.liveSyncPosition;
      if (Number.isFinite(livePos) && livePos > 0) {
        console.log(
          `Seeking video to HLS live sync position: ${livePos.toFixed(1)}s`,
        );
        this.video.currentTime = livePos;
        return;
      }
    }

    // Fallback to seekable range
    const end = this.liveEdge(this.video);
    if (Number.isNaN(end)) {
      return;
    }
    // Seek very close to live edge (just 0.1s back to avoid buffering issues)
    const targetTime = Math.max(0, end - 0.1);
    console.log(
      `Seeking video to live edge: ${targetTime.toFixed(1)}s (end: ${end.toFixed(1)}s)`,
    );
    this.video.currentTime = targetTime;
  }

  setVolume(value) {
    this.audio.volume = value / 100;
  }

  updatePlayButton(playing, mode = this.playbackMode) {
    const icon = this.playPauseBtn.querySelector("i");
    if (playing) {
      icon.className = "fas fa-pause";
    } else {
      icon.className = "fas fa-play";
    }

    if (mode === "video") {
      this.isVideoPlaying = playing;
    } else {
      this.isPlaying = playing;
    }
  }

  showLoadingState() {
    // Only show loading state when actually trying to connect
    if (this.playbackMode === "audio" && this.isPlaying) {
      document.getElementById("current-track").textContent = "Loading...";
      document.getElementById("current-artist").textContent =
        "Connecting to stream";
    }
  }

  hideLoadingState() {
    // Don't clear here - will be updated by real metadata when available
  }

  setInitialTrackState() {
    // Set a better initial state
    document.getElementById("current-track").textContent =
      "Click Play to start";
    const stationName = this.config?.station?.station_name || "Radio";
    document.getElementById("current-artist").textContent = stationName;
  }

  handleAudioError(error) {
    if (this.playbackMode !== "audio") {
      console.log("Audio error while not in audio mode - ignoring", error);
      return;
    }
    console.error("Audio error:", error);
    console.log("Attempting automatic reconnection...");

    // Show connecting state
    document.getElementById("current-track").textContent = "Reconnecting...";
    document.getElementById("current-artist").textContent =
      "Stream disconnected - attempting to reconnect";

    this.updatePlayButton(false, "audio");

    // Start reconnection attempts
    this.startReconnectionAttempts();
  }

  startReconnectionAttempts() {
    if (this.playbackMode !== "audio") {
      return;
    }
    if (this.reconnectionTimer) {
      clearTimeout(this.reconnectionTimer);
    }

    let attemptCount = 0;
    const maxAttempts = 3; // Initial rapid attempts before backing off

    const attemptReconnection = () => {
      if (this.playbackMode !== "audio") {
        if (this.reconnectionTimer) {
          clearTimeout(this.reconnectionTimer);
          this.reconnectionTimer = null;
        }
        return;
      }
      attemptCount++;
      console.log(`Reconnection attempt ${attemptCount}/${maxAttempts}`);

      // Update status
      document.getElementById("current-artist").textContent =
        `Reconnecting... (${attemptCount}/${maxAttempts})`;

      // Simple stream reload
      this.forceStreamReload();

      // Try to play after a short delay
      setTimeout(() => {
        const testPlayPromise = this.audio.play();

        if (testPlayPromise !== undefined) {
          testPlayPromise
            .then(() => {
              console.log("Reconnection successful!");
              this.isPlaying = true;
              this.updatePlayButton(true, "audio");
              this.consecutiveErrors = 0;
              if (this.reconnectionTimer) {
                clearTimeout(this.reconnectionTimer);
                this.reconnectionTimer = null;
              }
            })
            .catch((error) => {
              console.log(
                `Reconnection attempt ${attemptCount} failed:`,
                error,
              );

              if (attemptCount < maxAttempts) {
                console.log(`Next attempt in 5 seconds`);
                this.reconnectionTimer = setTimeout(attemptReconnection, 5000);
              } else {
                console.log(
                  "Initial reconnection attempts failed - backing off",
                );
                document.getElementById("current-track").textContent =
                  "Reconnecting...";
                document.getElementById("current-artist").textContent =
                  "Waiting for stream to return";
                this.reconnectionTimer = setTimeout(attemptReconnection, 15000);
              }
            });
        } else {
          this.isPlaying = true;
          this.updatePlayButton(true, "audio");
          this.consecutiveErrors = 0;
        }
      }, 1000);
    };

    // Start first attempt after a short delay
    this.reconnectionTimer = setTimeout(attemptReconnection, 2000);
  }

  startStreamHealthCheck() {
    this.consecutiveErrors = 0;
    this.consecutiveHealthCheckFailures = 0;
    this.lastPlaybackTime = 0;

    // Check stream health every 60 seconds for local streams (very conservative)
    this.healthCheckInterval = setInterval(() => {
      if (this.isPlaying && !this.reconnectionTimer) {
        this.checkStreamHealth();
        // Only check audio progress very occasionally for local streams
        if (Math.random() < 0.1) {
          // 10% chance
          this.checkAudioProgress();
        }
      }
    }, 60000);
  }

  attemptAutoStart() {
    // Auto-start only if user hasn't interacted yet
    setTimeout(() => {
      if (
        this.playbackMode === "audio" &&
        !this.isPlaying &&
        !this.hasUserInteracted
      ) {
        console.log("Attempting auto-start...");
        document.getElementById("current-track").textContent =
          "Auto-starting...";
        this.togglePlayPause();
      }
    }, 3000);
  }

  checkAudioProgress() {
    if (!this.isPlaying || this.reconnectionTimer) return;

    // Simple check: if audio element shows no source
    if (
      this.audio.readyState === 0 ||
      this.audio.networkState === HTMLMediaElement.NETWORK_NO_SOURCE
    ) {
      console.warn("Audio element shows no source - stream may be down");
      this.handleAudioError(new Error("Audio stream appears to be down"));
    }
  }

  async checkStreamHealth() {
    try {
      console.log("🔍 Checking stream health...");

      // First check: Test if the radio server API is responding
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5000); // Longer timeout

      const response = await fetch("/api/now-playing", {
        method: "GET",
        signal: controller.signal,
      });

      clearTimeout(timeoutId);

      if (!response.ok) {
        throw new Error("Server API not responding");
      }

      // Less strict content check - only fail if we get actual error indicators
      const data = await response.json();
      const isErrorState =
        !data.title ||
        data.title === "" ||
        (data.title === "Unknown Title" &&
          data.artist === "Unknown Artist" &&
          !data.listeners);

      if (isErrorState) {
        throw new Error(
          "Stream appears to be down - no valid metadata. Current: " +
            JSON.stringify(data),
        );
      }

      // Only check audio element if it's clearly broken
      if (this.audio.error && this.audio.error.code !== 0) {
        throw new Error(`Audio element error: ${this.audio.error.code}`);
      }

      console.log("✅ Stream health check passed:", data);

      // Reset consecutive errors on successful check
      this.consecutiveErrors = 0;
    } catch (error) {
      console.warn("⚠️ Stream health check failed:", error.message);

      // Only trigger reconnection after many failed health checks for local streams
      this.consecutiveHealthCheckFailures =
        (this.consecutiveHealthCheckFailures || 0) + 1;

      if (
        this.consecutiveHealthCheckFailures >= 6 &&
        this.isPlaying &&
        !this.reconnectionTimer
      ) {
        console.log("Extended health check failures - initiating reconnection");
        this.handleAudioError(error);
      }
    }
  }

  handleMetadataError() {
    this.consecutiveErrors = (this.consecutiveErrors || 0) + 1;

    console.log(`📊 Metadata error count: ${this.consecutiveErrors}`);

    // Be very patient - only act after 8 consecutive errors (2+ minutes)
    if (
      this.consecutiveErrors >= 8 &&
      this.isPlaying &&
      !this.reconnectionTimer
    ) {
      console.log("Extended metadata fetch failures - checking stream health");
      this.checkStreamHealth();
    } else if (this.consecutiveErrors >= 4 && this.isPlaying) {
      // Show user we're aware but be less alarming
      document.getElementById("current-track").textContent =
        "Stream updating...";
      document.getElementById("current-artist").textContent = "Please wait";
    }
  }

  formatDJName(djName) {
    return djName.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase());
  }

  isTimeInRange(current, start, end) {
    const currentMinutes = this.timeToMinutes(current);
    const startMinutes = this.timeToMinutes(start);
    const endMinutes = this.timeToMinutes(end);

    if (startMinutes <= endMinutes) {
      return currentMinutes >= startMinutes && currentMinutes < endMinutes;
    } else {
      return currentMinutes >= startMinutes || currentMinutes < endMinutes;
    }
  }

  timeToMinutes(timeString) {
    const [hours, minutes] = timeString.split(":").map(Number);
    return hours * 60 + minutes;
  }

  startMetadataPolling() {
    // Poll for current track metadata every 15 seconds (less aggressive for local streams)
    this.metadataInterval = setInterval(async () => {
      try {
        const response = await fetch("/api/now-playing");
        if (response.ok) {
          const track = await response.json();

          // Only treat as placeholder if we get truly empty/invalid data
          const isActualError =
            !track.title ||
            track.title === "" ||
            (track.title === "Unknown Title" &&
              track.artist === "Unknown Artist" &&
              !track.listeners);

          if (isActualError && this.isPlaying) {
            console.log("📡 No valid track data received:", track);
            // Treat as a minor error but don't immediately panic
            this.consecutiveErrors = (this.consecutiveErrors || 0) + 1;

            // Only show error state if we've had multiple failures
            if (this.consecutiveErrors >= 3) {
              document.getElementById("current-track").textContent =
                "Stream updating...";
              document.getElementById("current-artist").textContent =
                "Please wait";
            }
          } else {
            // We have valid data - update normally
            this.updateNowPlaying(track);
            // Reset error counters on successful update
            this.consecutiveErrors = 0;
            this.consecutiveHealthCheckFailures = 0;
          }
        } else {
          this.handleMetadataError();
        }
      } catch (error) {
        console.error("Error fetching track metadata:", error);
        this.handleMetadataError();
      }
    }, 15000); // Reduced frequency for local streams

    // Start stream health monitoring
    this.startStreamHealthCheck();

    // Poll for timeline updates every 15 seconds
    this.timelineInterval = setInterval(async () => {
      try {
        await this.loadTimeline();
      } catch (error) {
        console.error("Error updating timeline:", error);
      }
    }, 15000);

    // Poll for current show updates every 15 seconds (more frequent for better updates)
    this.showInterval = setInterval(async () => {
      try {
        const response = await fetch("/api/current-show");
        if (response.ok) {
          const newShow = await response.json();

          // Only update if show has actually changed
          if (
            !this.currentShow ||
            this.currentShow.dj_name !== newShow.dj_name
          ) {
            console.log("Show changed:", newShow.dj_name);
            this.currentShow = newShow;
            this.updateCurrentShow();
          } else {
            // Update existing show data (in case times changed)
            this.currentShow = newShow;
            this.updateCurrentShow();
          }
        }
      } catch (error) {
        console.error("Error updating current show:", error);
      }
    }, 15000);

    // Poll for weather updates every 10 minutes
    this.weatherInterval = setInterval(async () => {
      try {
        await this.updateWeather();
      } catch (error) {
        console.error("Error updating weather:", error);
      }
    }, 600000);

    // Poll for listener count every 30 seconds
    this.listenerInterval = setInterval(async () => {
      try {
        await this.updateListenerCount();
      } catch (error) {
        console.error("Error updating listener count:", error);
      }
    }, 30000);
  }

  stopMetadataPolling() {
    if (this.metadataInterval) {
      clearInterval(this.metadataInterval);
      this.metadataInterval = null;
    }
    if (this.timelineInterval) {
      clearInterval(this.timelineInterval);
      this.timelineInterval = null;
    }
    if (this.timelineTicker) {
      clearInterval(this.timelineTicker);
      this.timelineTicker = null;
    }
    if (this.showInterval) {
      clearInterval(this.showInterval);
      this.showInterval = null;
    }
    if (this.weatherInterval) {
      clearInterval(this.weatherInterval);
      this.weatherInterval = null;
    }
    if (this.listenerInterval) {
      clearInterval(this.listenerInterval);
      this.listenerInterval = null;
    }
    if (this.healthCheckInterval) {
      clearInterval(this.healthCheckInterval);
      this.healthCheckInterval = null;
    }
    if (this.videoHealthCheckInterval) {
      clearInterval(this.videoHealthCheckInterval);
      this.videoHealthCheckInterval = null;
    }
  }

  startVideoHealthCheck() {
    // Monitor video stream health every 5 seconds
    this.videoHealthCheckInterval = setInterval(() => {
      if (this.playbackMode !== "video" || !this.video) {
        return;
      }

      if (document.hidden) {
        return;
      }

      if (!this.isVideoPlaying) {
        if (
          !this.expectingVideoPause &&
          !this.videoRecoveryTimer &&
          (!this.videoRestartGraceUntil ||
            Date.now() >= this.videoRestartGraceUntil)
        ) {
          this.scheduleVideoRecovery("not playing");
        }
        return;
      }

      const currentTime = this.video.currentTime;

      if (this.video.paused) {
        return;
      }

      if (
        this.videoRestartGraceUntil &&
        Date.now() < this.videoRestartGraceUntil
      ) {
        return;
      }

      const now = Date.now();
      if (this.videoLastProgressAt && now - this.videoLastProgressAt < 6000) {
        return;
      }

      let bufferAhead = 0;
      if (this.video.buffered && this.video.buffered.length > 0) {
        const end = this.video.buffered.end(this.video.buffered.length - 1);
        bufferAhead = end - currentTime;
      }

      if (bufferAhead <= 0.5) {
        this.videoStallCount = 0;
        this.lastVideoProgress = currentTime;
        return;
      }

      const progressDelta = Math.abs(currentTime - this.lastVideoProgress);

      // Check if video is progressing
      if (progressDelta < 0.01 && !this.video.paused) {
        this.videoStallCount++;
        console.log(`Video stall detected (${this.videoStallCount}/3)`);

        if (this.videoStallCount >= 3) {
          console.log("Video stream appears stalled - attempting recovery");
          this.updateVideoStatus("Recovering video stream...");
          this.videoStallCount = 0;

          // Try to recover
          if (this.hlsInstance) {
            this.hlsInstance.startLoad();
            // If still stalled after startLoad, recreate the stream
            setTimeout(() => {
              if (
                this.video.currentTime === currentTime &&
                this.videoPlaylistUrl
              ) {
                console.log("Still stalled - recreating HLS instance");
                this.setupVideoStream(this.videoPlaylistUrl);
                setTimeout(() => this.startVideoPlayback(), 1000);
              }
            }, 3000);
          }
        }
      } else {
        this.videoStallCount = 0;
        this.lastVideoProgress = currentTime;
      }
    }, 5000);
  }

  stopReconnectionTimer() {
    if (this.reconnectionTimer) {
      clearTimeout(this.reconnectionTimer);
      this.reconnectionTimer = null;
    }
  }

  queueVideoRestart(reason, shouldResume = false) {
    if (
      !this.video ||
      this.playbackMode !== "video" ||
      !this.videoPlaylistUrl
    ) {
      return;
    }

    if (this.videoRestartTimer) {
      clearTimeout(this.videoRestartTimer);
      this.videoRestartTimer = null;
    }

    this.resetVideoRecovery(reason);
    this.updateVideoStatus("Reconnecting video stream...");

    this.videoRestartTimer = setTimeout(() => {
      this.videoRestartTimer = null;
      this.setupVideoStream(this.videoPlaylistUrl, { force: true });
      if (shouldResume) {
        if (this.hasUserInteracted) {
          this.startVideoPlayback();
        } else {
          this.startVideoPlayback({ auto: true });
        }
      }
    }, 2000);
  }

  queueVideoAutoStart(autoStart) {
    if (this.playbackMode !== "video") {
      return;
    }
    this.videoAutoStartPending = true;
    this.videoAutoStartRequestedAuto = autoStart === true;
  }

  maybeStartPendingVideo() {
    if (!this.videoAutoStartPending || this.playbackMode !== "video") {
      return;
    }
    this.videoAutoStartPending = false;
    const shouldAuto = this.videoAutoStartRequestedAuto;
    this.videoAutoStartRequestedAuto = false;
    if (shouldAuto) {
      this.startVideoPlayback({ auto: true });
    } else {
      this.startVideoPlayback();
    }
  }
}

// Weather and additional features
class WeatherDisplay {
  constructor() {
    this.weatherDisplay = document.getElementById("weather-display");
    this.weatherText = document.getElementById("weather-text");
    this.updateWeather();

    // Update weather every 10 minutes
    setInterval(() => this.updateWeather(), 10 * 60 * 1000);
  }

  async updateWeather() {
    try {
      // This would typically come from the radio server
      // For now, we'll show a placeholder
      this.weatherText.textContent = "Weather updates coming soon";
    } catch (error) {
      console.error("Error updating weather:", error);
      this.weatherText.textContent = "Weather unavailable";
    }
  }
}

// Listener count (simulated)
class ListenerCounter {
  constructor() {
    this.listenerCountElement = document.getElementById("listener-count");
    this.updateListenerCount();

    // Update every 30 seconds
    setInterval(() => this.updateListenerCount(), 30000);
  }

  updateListenerCount() {
    // Simulate listener count (in a real implementation, this would come from the audio stream)
    const count = Math.floor(Math.random() * 150) + 50;
    this.listenerCountElement.textContent = `Listeners: ${count}`;
  }
}

// Initialize everything when DOM is loaded
document.addEventListener("DOMContentLoaded", () => {
  new RadioPlayer();
  new WeatherDisplay();
  new ListenerCounter();

  console.log("Radio web interface initialized");
});
