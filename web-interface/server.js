const express = require("express");
const http = require("http");
const socketIo = require("socket.io");
const path = require("path");
const fs = require("fs");
const axios = require("axios");
const cors = require("cors");
const helmet = require("helmet");
const compression = require("compression");

const app = express();
const server = http.createServer(app);
const io = socketIo(server, {
  cors: {
    origin: "*",
    methods: ["GET", "POST"],
  },
});

const PORT = process.env.PORT || 3000;
const RADIO_STREAM_URL =
  process.env.RADIO_STREAM_URL || "http://localhost:3000/audio/live.m3u8";
const VIDEO_STREAM_URL = process.env.VIDEO_STREAM_URL || "/video/live.m3u8";

// Track video viewers - Map of IP addresses to last access timestamp
const videoViewers = new Map();
// Track audio listeners - Map of IP addresses to last access timestamp
const audioListeners = new Map();
const VIEWER_TIMEOUT_MS = 30000; // Consider viewer inactive after 30 seconds

function cleanupListeners() {
  const now = Date.now();
  for (const [ip, lastAccess] of videoViewers.entries()) {
    if (now - lastAccess > VIEWER_TIMEOUT_MS) {
      videoViewers.delete(ip);
    }
  }
  for (const [ip, lastAccess] of audioListeners.entries()) {
    if (now - lastAccess > VIEWER_TIMEOUT_MS) {
      audioListeners.delete(ip);
    }
  }
}

function getListenerSummary() {
  cleanupListeners();

  const allListeners = new Set([
    ...videoViewers.keys(),
    ...audioListeners.keys(),
  ]);

  return {
    count: allListeners.size,
    video_viewers: videoViewers.size,
    audio_listeners: audioListeners.size,
  };
}

function parseQuotedValue(value) {
  if (!value) {
    return "";
  }
  if (value.startsWith('"') && value.endsWith('"')) {
    return value.slice(1, -1).replace(/\\"/g, '"').replace(/\\\\/g, "\\");
  }
  return value;
}

function parseDaterangeAttributes(line) {
  const raw = line.replace("#EXT-X-DATERANGE:", "").trim();
  const attrs = {};
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < raw.length; i += 1) {
    const char = raw[i];
    if (char === '"') {
      inQuotes = !inQuotes;
    }
    if (char === "," && !inQuotes) {
      if (current.trim()) {
        const idx = current.indexOf("=");
        if (idx !== -1) {
          const key = current.slice(0, idx).trim();
          const value = current.slice(idx + 1).trim();
          attrs[key] = parseQuotedValue(value);
        }
      }
      current = "";
      continue;
    }
    current += char;
  }

  if (current.trim()) {
    const idx = current.indexOf("=");
    if (idx !== -1) {
      const key = current.slice(0, idx).trim();
      const value = current.slice(idx + 1).trim();
      attrs[key] = parseQuotedValue(value);
    }
  }

  return attrs;
}

function parseNowPlayingFromPlaylist(playlistText) {
  if (!playlistText) {
    return null;
  }

  const lines = playlistText.split(/\r?\n/);
  const events = [];
  for (const line of lines) {
    if (!line.startsWith("#EXT-X-DATERANGE:")) {
      continue;
    }
    const attrs = parseDaterangeAttributes(line);
    if (
      attrs.CLASS &&
      attrs.CLASS.toLowerCase() === "now-playing"
    ) {
      events.push({
        title: attrs["X-TITLE"] || "Unknown Title",
        artist: attrs["X-ARTIST"] || "Unknown Artist",
        album: attrs["X-ALBUM"] || "",
        start: attrs["START-DATE"] || "",
        artwork_url: attrs["X-ARTWORK-URL"] || attrs["X-IMAGE"] || "",
      });
    }
  }

  if (events.length === 0) {
    return null;
  }

  // The newest event is the encoder's edge; the full list lets a client that is
  // a buffer behind it pick the track it is actually hearing.
  return { ...events[events.length - 1], events };
}

async function getAudioNowPlaying() {
  try {
    const response = await axios.get(
      "http://radio-server:8080/audio/live.m3u8",
      {
        timeout: 3000,
        headers: { "Cache-Control": "no-cache" },
      },
    );
    const parsed = parseNowPlayingFromPlaylist(response.data);
    if (parsed && parsed.title) {
      return parsed;
    }
  } catch (error) {
    console.log("Audio playlist unavailable:", error.message);
  }
  return null;
}

// Clean up stale viewers/listeners periodically
setInterval(() => {
  cleanupListeners();
}, 10000); // Clean up every 10 seconds

// Middleware
app.use(
  helmet({
    contentSecurityPolicy: {
      directives: {
        defaultSrc: ["'self'"],
        // Every host the page actually loads from: blocking one of these fails
        // silently — hls.js not loading leaves the stream unplayable in Chrome,
        // and a blocked webfont renders the icons as empty boxes.
        styleSrc: [
          "'self'",
          "'unsafe-inline'",
          "https://cdnjs.cloudflare.com",
          "https://fonts.googleapis.com",
        ],
        scriptSrc: [
          "'self'",
          "'unsafe-inline'",
          "https://cdnjs.cloudflare.com",
          "https://cdn.jsdelivr.net",
        ],
        fontSrc: [
          "'self'",
          "https://cdnjs.cloudflare.com",
          "https://fonts.gstatic.com",
        ],
        imgSrc: ["'self'", "data:"],
        // hls.js hands the player a MediaSource blob: URL and runs its
        // demuxer in a blob: worker — both are blocked without these.
        mediaSrc: ["'self'", "blob:"],
        workerSrc: ["'self'", "blob:"],
        connectSrc: ["'self'", "ws:", "wss:"],
      },
    },
  }),
);
const shouldCompress = (req, res) => {
  if (
    req.originalUrl.startsWith("/video/") ||
    req.originalUrl.startsWith("/audio/")
  ) {
    return false;
  }
  return compression.filter(req, res);
};

app.use(
  compression({
    filter: shouldCompress,
  }),
);
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, "public")));

// No local config caching - get everything from radio-server API

app.get("/stream.mp3", (req, res) => {
  const headers = {};
  if (req.headers["icy-metadata"] === "1") {
    headers["Icy-MetaData"] = "1";
  }
  if (req.headers.host) {
    headers["X-Forwarded-Host"] = req.headers.host;
  }
  headers["X-Forwarded-Proto"] = req.protocol || "http";
  if (req.ip) {
    headers["X-Forwarded-For"] = req.ip;
  }

  const upstreamReq = http.request(
    {
      hostname: "radio-server",
      port: 8080,
      path: "/stream.mp3",
      method: "GET",
      headers,
    },
    (upstreamRes) => {
      res.status(upstreamRes.statusCode || 502);
      for (const [key, value] of Object.entries(upstreamRes.headers)) {
        if (typeof value !== "undefined") {
          res.setHeader(key, value);
        }
      }
      upstreamRes.pipe(res);
    },
  );

  upstreamReq.on("error", (error) => {
    console.error("Error proxying /stream.mp3:", error.message);
    if (!res.headersSent) {
      res.status(502);
    }
    res.end();
  });

  req.on("close", () => {
    upstreamReq.destroy();
  });

  upstreamReq.end();
});

app.get("/artwork/*", (req, res) => {
  const upstreamReq = http.request(
    {
      hostname: "radio-server",
      port: 8080,
      path: req.originalUrl,
      method: "GET",
    },
    (upstreamRes) => {
      res.status(upstreamRes.statusCode || 502);
      for (const [key, value] of Object.entries(upstreamRes.headers)) {
        if (typeof value !== "undefined") {
          res.setHeader(key, value);
        }
      }
      upstreamRes.pipe(res);
    },
  );

  upstreamReq.on("error", (error) => {
    console.error("Error proxying /artwork:", error.message);
    if (!res.headersSent) {
      res.status(502);
    }
    res.end();
  });

  req.on("close", () => {
    upstreamReq.destroy();
  });

  upstreamReq.end();
});

// API Routes
app.get("/api/config", async (req, res) => {
  try {
    // Get config from radio-server
    const stationResponse = await axios.get(
      "http://radio-server:8080/api/station",
      {
        timeout: 5000,
      },
    );
    const scheduleResponse = await axios.get(
      "http://radio-server:8080/api/schedule",
      {
        timeout: 5000,
      },
    );

    const playlistPath = normalizeVideoPath(
      stationResponse.data?.video_stream?.playlist,
    );
    const audioPlaylist =
      stationResponse.data?.audio_stream?.playlist || RADIO_STREAM_URL;

    res.json({
      station: stationResponse.data,
      schedule: scheduleResponse.data,
      stream_url: audioPlaylist,
      video_stream_url: playlistPath,
    });
  } catch (error) {
    console.error("Error fetching config from radio-server:", error.message);
    res.status(503).json({
      error: "Radio server unavailable",
      message: error.message,
    });
  }
});

app.get("/api/current-show", async (req, res) => {
  try {
    // Get current show from timeline API
    const timelineResponse = await axios.get(
      "http://radio-server:8080/api/timeline/current",
      { timeout: 5000 },
    );

    if (timelineResponse.data && timelineResponse.data.dj_id) {
      const timeline = timelineResponse.data;

      // Get schedule from radio-server to find music_folders
      const scheduleResponse = await axios.get(
        "http://radio-server:8080/api/schedule",
        { timeout: 5000 },
      );

      let scheduleEntry = null;
      if (scheduleResponse.data && scheduleResponse.data.schedule) {
        scheduleEntry = scheduleResponse.data.schedule.find(
          (entry) => entry.dj_name === timeline.dj_id,
        );
      }

      // Create enriched show info combining timeline and schedule data
      const startTime = scheduleEntry
        ? scheduleEntry.start_time
        : new Date(timeline.show_start).toLocaleTimeString("en-GB", {
            hour: "2-digit",
            minute: "2-digit",
            timeZone: "Europe/London",
          });
      const endTime = scheduleEntry
        ? scheduleEntry.end_time
        : new Date(timeline.show_end).toLocaleTimeString("en-GB", {
            hour: "2-digit",
            minute: "2-digit",
            timeZone: "Europe/London",
          });

      const currentShow = {
        dj_name: timeline.dj_id,
        dj_id: timeline.dj_id,
        start_time: startTime,
        end_time: endTime,
        music_folders: scheduleEntry
          ? scheduleEntry.music_folders
          : ["Various"],
        timeline_id: timeline.timeline_id,
        current_time: timeline.current_time,
      };

      console.log(`Current show from timeline: ${currentShow.dj_name}`);
      return res.json(currentShow);
    }
  } catch (error) {
    console.log("Timeline API unavailable:", error.message);
  }

  // If timeline API fails, return error
  res.status(503).json({
    error: "Radio server unavailable",
    message: "Unable to fetch current show information",
  });
});

app.get("/api/next-show", async (req, res) => {
  try {
    // Get schedule from radio-server
    const scheduleResponse = await axios.get(
      "http://radio-server:8080/api/schedule",
      { timeout: 5000 },
    );

    if (!scheduleResponse.data || !scheduleResponse.data.schedule) {
      return res.status(404).json({ error: "Schedule not available" });
    }

    const schedule = scheduleResponse.data;
    const now = new Date();
    const currentTime = now.toTimeString().slice(0, 5);

    // Find next show
    const sortedSchedule = schedule.schedule.sort(
      (a, b) => timeToMinutes(a.start_time) - timeToMinutes(b.start_time),
    );

    const currentMinutes = timeToMinutes(currentTime);
    const nextShow =
      sortedSchedule.find(
        (entry) => timeToMinutes(entry.start_time) > currentMinutes,
      ) || sortedSchedule[0]; // If no show later today, return first show of tomorrow

    res.json(nextShow);
  } catch (error) {
    console.error("Error fetching schedule from radio-server:", error.message);
    res.status(503).json({
      error: "Radio server unavailable",
      message: error.message,
    });
  }
});

app.get("/api/schedule", async (req, res) => {
  try {
    console.log("Schedule API requested");
    // Get schedule from radio-server with increased timeout
    const response = await axios.get("http://radio-server:8080/api/schedule", {
      timeout: 10000, // Increased from 5s to 10s
    });

    if (!response.data) {
      throw new Error("Empty schedule data received");
    }

    console.log("Schedule fetched successfully");
    res.json(response.data);
  } catch (error) {
    console.error("Error fetching schedule from radio-server:", error.message);

    // Return a more detailed error
    res.status(503).json({
      error: "Radio server unavailable",
      message: error.message,
      timestamp: new Date().toISOString(),
    });
  }
});

// API endpoint to report video viewer count (for radio-server to check)
app.get("/api/video-viewers", (req, res) => {
  const summary = getListenerSummary();

  res.json({
    count: summary.count,
    video_viewers: summary.video_viewers,
    audio_listeners: summary.audio_listeners,
    timestamp: new Date().toISOString(),
  });
});

app.get("/api/now-playing", async (req, res) => {
  try {
    const listenerSummary = getListenerSummary();

    try {
      const audioNowPlaying = await getAudioNowPlaying();
      if (audioNowPlaying) {
        res.json({
          ...audioNowPlaying,
          listeners: listenerSummary.count,
        });
        return;
      }
    } catch (radioError) {
      console.log(
        "Audio playlist parse failed:",
        radioError.message,
      );
    }

    const radioServerResponse = await axios.get(
      "http://radio-server:8080/api/current-track",
      { timeout: 3000 },
    );
    if (
      radioServerResponse.data &&
      radioServerResponse.data.title &&
      radioServerResponse.data.title !== "Unknown Title"
    ) {
      res.json({
        ...radioServerResponse.data,
        listeners: listenerSummary.count,
      });
      return;
    }

    res.json({
      title: "Stream Starting...",
      artist: "Radio",
      listeners: listenerSummary.count,
    });
  } catch (error) {
    console.error("Error fetching now playing:", error.message);
    const listenerSummary = getListenerSummary();
    res.json({
      title: "Unknown Title",
      artist: "Unknown Artist",
      listeners: listenerSummary.count,
    });
  }
});

// Debug endpoint to show current system status and issues
app.get("/api/debug-status", (req, res) => {
  res.json({
    status: "debug",
    timestamp: new Date().toISOString(),
    current_issues: {
      "1_dj_audio_generation": {
        status: "working",
        description:
          "DJ audio files are being generated successfully with proper song information",
        evidence:
          "Files like dj_speech_1758961534.mp3 created with song-specific prompts",
        example_prompt:
          "Share an interesting fact about this song...The song is 'Uncle Walter' by Ben Folds Five from the album 'Ben Folds Five' (2000).",
      },
      "2_timing_coordination": {
        status: "broken",
        description:
          "DJ talk is generated for one song but a different song plays",
        evidence:
          "Generated talk about 'Uncle Walter' but 'Underground' played instead",
        root_cause:
          "No timeline-based scheduling system to coordinate what plays when",
      },
      "3_redundant_generation": {
        status: "broken",
        description:
          "Multiple DJ files generated per song block - both specific intros and general chat",
        evidence:
          "Generated both song-specific fact sharing AND general morning chat for same time slot",
        root_cause: "Scheduler creating multiple segments without coordination",
      },
      "4_playback_integration": {
        status: "partially_working",
        description:
          "DJ audio files are queued but timing is wrong - plays after different song queued",
        evidence:
          "DJ talk about Uncle Walter queued AFTER Underground was already queued",
        root_cause: "No pre-planning of playlist with proper intro timing",
      },
    },
    proposed_solution: {
      timeline_queue_system:
        "Replace current reactive scheduling with timeline-based planning where each item has a specific timestamp",
      advance_preparation:
        "Generate DJ audio 60+ seconds before needed, with proper song context",
      coordination_layer:
        "Timeline queue manages what plays when, preventing conflicts and ensuring proper order",
    },
    openai_debugging: {
      text_generation_working: true,
      speech_generation_working: true,
      prompt_injection_working: true,
      sample_working_prompt:
        "You are morning_mike...Share an interesting fact about this song...The song is 'Uncle Walter' by Ben Folds Five...",
      sample_response:
        "Hey there, morning crew! Up next we've got 'Uncle Walter' by Ben Folds Five. Did you know this track was inspired by a quirky family member of Ben's?",
    },
  });
});

app.get("/api/timeline", async (req, res) => {
  try {
    // Fetch timeline from the radio server API
    const response = await axios.get(
      "http://radio-server:8080/api/timeline/current",
      {
        timeout: 5000,
      },
    );

    res.json(response.data);
  } catch (error) {
    console.error("Error fetching timeline:", error.message);
    res.status(503).json({
      error: "Timeline service unavailable",
      details: error.message,
    });
  }
});

app.get("/api/timeline/upcoming", async (req, res) => {
  try {
    const count = req.query.count || 10;
    const response = await axios.get(
      `http://radio-server:8080/api/timeline/upcoming?count=${count}`,
      {
        timeout: 5000,
      },
    );

    res.json(response.data);
  } catch (error) {
    console.error("Error fetching upcoming timeline:", error.message);
    res.status(503).json({
      error: "Timeline service unavailable",
      details: error.message,
    });
  }
});

app.get("/api/weather", async (req, res) => {
  try {
    // Try to get weather from radio server
    const response = await axios.get("http://radio-server:8080/api/weather", {
      timeout: 5000,
    });

    if (response.data) {
      res.json(response.data);
    } else {
      throw new Error("No weather data received");
    }
  } catch (error) {
    console.error("Error fetching weather from radio server:", error.message);
    // Fallback to mock data
    res.json({
      condition: "Weather Unavailable",
      temperature: "--",
      location: "London",
    });
  }
});

app.get("/api/dj-files", async (req, res) => {
  try {
    const { exec } = require("child_process");
    const util = require("util");
    const execPromise = util.promisify(exec);

    // Get list of DJ files
    const { stdout: fileList } = await execPromise(
      "docker compose exec -T radio-server ls -lt /app/temp-audio/ | head -10",
    );

    // Get recent logs about DJ generation
    const { stdout: djLogs } = await execPromise(
      'docker compose logs radio-server --tail=50 | grep -E "(🎯 Generating DJ|✅ Generated DJ|🎤 SPEECH TEXT|✅ SPEECH FILE|Playing song)" | tail -20',
    );

    res.json({
      timestamp: new Date().toISOString(),
      recent_dj_files: fileList.split("\n").filter((line) => line.trim()),
      recent_activity: djLogs.split("\n").filter((line) => line.trim()),
      analysis: {
        file_path_bug_status: "FIXED - No more file paths in speech text",
        prompt_response_status: "WORKING - Clean prompt/response pairs visible",
        coordination_status:
          "TIMELINE_SCHEDULER_IMPLEMENTED - Now using timeline-based scheduling",
      },
    });
  } catch (error) {
    res.status(500).json({ error: error.message });
  }
});

// Timeline API proxy endpoints
app.get("/api/timeline/:endpoint", async (req, res) => {
  try {
    const endpoint = req.params.endpoint;
    const queryParams = new URLSearchParams(req.query).toString();
    const url = `http://radio-server:8080/api/timeline/${endpoint}${
      queryParams ? "?" + queryParams : ""
    }`;

    console.log(`Proxying timeline request to: ${url}`);

    const response = await axios.get(url, { timeout: 10000 });
    res.json(response.data);
  } catch (error) {
    console.error(
      `Timeline API proxy error for ${req.params.endpoint}:`,
      error.message,
    );

    if (error.response) {
      res.status(error.response.status).json(error.response.data);
    } else {
      res.status(503).json({
        error: "Timeline service unavailable",
        message: error.message,
      });
    }
  }
});

app.get("/health", (req, res) => {
  res.json({
    status: "healthy",
    timestamp: new Date().toISOString(),
    stream_url: RADIO_STREAM_URL,
  });
});

// Proxy video HLS playlist and segments via the web interface origin
app.get("/video/*", async (req, res) => {
  const resourcePath = req.params[0];
  const targetUrl = `http://radio-server:8080/video/${resourcePath}`;

  // Track viewer when they access the main playlist or segments
  if (resourcePath === "live.m3u8" || resourcePath.endsWith(".m3u8") || resourcePath.endsWith(".ts")) {
    const viewerIP = req.ip || req.connection.remoteAddress;
    videoViewers.set(viewerIP, Date.now());
    console.log(
      `📹 Video viewer tracked: ${viewerIP} (total active: ${videoViewers.size})`,
    );
  }

  try {
    const response = await axios({
      method: "GET",
      url: targetUrl,
      responseType: "stream",
      decompress: false,
      timeout: 15000,
      headers: {
        Range: req.headers.range,
      },
    });

    if (response.headers["content-type"]) {
      res.set("Content-Type", response.headers["content-type"]);
    }
    if (response.headers["content-length"]) {
      res.set("Content-Length", response.headers["content-length"]);
    }
    if (response.headers["accept-ranges"]) {
      res.set("Accept-Ranges", response.headers["accept-ranges"]);
    }

    res.set("Cache-Control", "no-cache, no-store");
    res.set("Access-Control-Allow-Origin", "*");
    res.set("Connection", "keep-alive");

    response.data.pipe(res);
  } catch (error) {
    const status = error.response?.status || 502;
    const message =
      error.response?.statusText || error.message || "Video stream unavailable";

    if (
      error.response?.data &&
      typeof error.response.data.pipe === "function"
    ) {
      res.status(status);
      error.response.data.pipe(res);
    } else if (error.response?.data) {
      res.status(status).send(error.response.data);
    } else {
      res.status(status).json({ error: "Video stream unavailable", message });
    }
  }
});

// Proxy audio HLS playlist and segments via the web interface origin
app.get("/audio/*", async (req, res) => {
  try {
    const resourcePath = req.params[0];
    const targetUrl = `http://radio-server:8080/audio/${resourcePath}`;

    // Track listener when they access the main playlist or segments
    if (resourcePath === "live.m3u8" || resourcePath.endsWith(".ts")) {
      const listenerIP = req.ip || req.connection.remoteAddress;
      audioListeners.set(listenerIP, Date.now());
      if (resourcePath === "live.m3u8") {
        console.log(
          `🎵 Audio listener tracked: ${listenerIP} (total active: ${audioListeners.size})`,
        );
      }
    }

    res.header("Access-Control-Allow-Origin", "*");
    res.header("Access-Control-Allow-Headers", "Range");
    res.header(
      "Access-Control-Expose-Headers",
      "Content-Length, Content-Range",
    );
    res.header("Cache-Control", "no-cache, no-store");
    res.header("Pragma", "no-cache");

    const response = await axios({
      method: "GET",
      url: targetUrl,
      responseType: "stream",
      headers: {
        "User-Agent": req.get("User-Agent") || "Web Player",
        "X-Proxy-Source": "web-interface",
        "X-Forwarded-For":
          req.headers["x-forwarded-for"] ||
          req.ip ||
          req.connection.remoteAddress,
      },
    });

    if (response.headers["content-type"]) {
      res.header("Content-Type", response.headers["content-type"]);
    }
    response.data.pipe(res);
  } catch (error) {
    console.error("Error proxying audio HLS:", error.message);
    res.status(503).json({ error: "Audio stream temporarily unavailable" });
  }
});

// Utility functions
function isTimeInRange(current, start, end) {
  const currentMinutes = timeToMinutes(current);
  const startMinutes = timeToMinutes(start);
  const endMinutes = timeToMinutes(end);

  if (startMinutes <= endMinutes) {
    // Same day range
    return currentMinutes >= startMinutes && currentMinutes < endMinutes;
  } else {
    // Overnight range (crosses midnight)
    return currentMinutes >= startMinutes || currentMinutes < endMinutes;
  }
}

function timeToMinutes(timeString) {
  const [hours, minutes] = timeString.split(":").map(Number);
  return hours * 60 + minutes;
}

function normalizeVideoPath(pathValue) {
  if (!pathValue) {
    return VIDEO_STREAM_URL;
  }

  if (pathValue.startsWith("http")) {
    try {
      const parsed = new URL(pathValue);
      if (parsed.pathname) {
        return parsed.pathname + parsed.search;
      }
    } catch (error) {
      console.warn("Unable to normalize video path:", error.message);
      return VIDEO_STREAM_URL;
    }
  }

  return pathValue;
}

// Cache for schedule to detect changes
let lastScheduleHash = null;

function getScheduleHash(schedule) {
  return JSON.stringify(schedule);
}

// Socket.IO for real-time updates
let radioServerAvailable = true;
let lastRadioServerCheck = Date.now();

io.on("connection", async (socket) => {
  console.log("Client connected:", socket.id);

  // Send current radio server status
  socket.emit("server-status", { available: radioServerAvailable });

  // Send current configuration to new client from radio-server
  try {
    const configResponse = await axios.get(
      "http://radio-server:8080/api/config",
      {
        timeout: 5000,
      },
    );
    const audioStreamUrl =
      configResponse.data?.station?.audio_stream?.playlist || RADIO_STREAM_URL;
    socket.emit("config", {
      station: configResponse.data.station,
      schedule: configResponse.data.schedule,
      stream_url: audioStreamUrl,
      video_stream_url: normalizeVideoPath(
        configResponse.data?.station?.video_stream?.playlist,
      ),
    });
  } catch (error) {
    console.error("Error fetching config for new client:", error.message);
    // Notify client that server is unavailable
    socket.emit("server-status", { available: false });
  }

  socket.on("disconnect", () => {
    console.log("Client disconnected:", socket.id);
  });
});

// Periodic updates - fetch from timeline API
setInterval(async () => {
  try {
    // Get current show from timeline API
    const timelineResponse = await axios.get(
      "http://radio-server:8080/api/timeline/current",
      { timeout: 5000 },
    );

    if (timelineResponse.data && timelineResponse.data.dj_id) {
      const timeline = timelineResponse.data;

      // Get schedule from radio-server to find music_folders
      const scheduleResponse = await axios.get(
        "http://radio-server:8080/api/schedule",
        { timeout: 5000 },
      );

      let scheduleEntry = null;
      if (scheduleResponse.data && scheduleResponse.data.schedule) {
        scheduleEntry = scheduleResponse.data.schedule.find(
          (entry) => entry.dj_name === timeline.dj_id,
        );
      }

      // Create enriched show info
      const currentShow = {
        dj_name: timeline.dj_id,
        dj_id: timeline.dj_id,
        start_time: new Date(timeline.show_start).toLocaleTimeString("en-GB", {
          hour: "2-digit",
          minute: "2-digit",
          timeZone: "Europe/London",
        }),
        end_time: new Date(timeline.show_end).toLocaleTimeString("en-GB", {
          hour: "2-digit",
          minute: "2-digit",
          timeZone: "Europe/London",
        }),
        music_folders: scheduleEntry
          ? scheduleEntry.music_folders
          : ["Various"],
        timeline_id: timeline.timeline_id,
        current_time: timeline.current_time,
      };

      io.emit("current-show", currentShow);
    }

    // Check if schedule has changed and emit update if needed
    try {
      const scheduleResponse = await axios.get(
        "http://radio-server:8080/api/schedule",
        { timeout: 5000 },
      );

      if (scheduleResponse.data) {
        const scheduleHash = getScheduleHash(scheduleResponse.data);
        if (scheduleHash !== lastScheduleHash) {
          console.log("Schedule changed, emitting update to clients");
          lastScheduleHash = scheduleHash;

          // Fetch station config and emit full config update
          const stationResponse = await axios.get(
            "http://radio-server:8080/api/station",
            { timeout: 5000 },
          );
          const audioStreamUrl =
            stationResponse.data?.audio_stream?.playlist || RADIO_STREAM_URL;

          io.emit("config", {
            station: stationResponse.data,
            schedule: scheduleResponse.data,
            stream_url: audioStreamUrl,
            video_stream_url: normalizeVideoPath(
              stationResponse.data?.video_stream?.playlist,
            ),
          });
        }
      }
    } catch (error) {
      console.log("Error checking schedule updates:", error.message);
    }
  } catch (error) {
    console.log(
      "Timeline API unavailable for Socket.IO update:",
      error.message,
    );
    // Radio server is down - notify clients if status changed
    if (radioServerAvailable) {
      radioServerAvailable = false;
      console.log("Radio server became unavailable - notifying clients");
      io.emit("server-status", { available: false });
    }
  }
}, 30000); // Update every 30 seconds

// More frequent health check for radio-server availability
setInterval(async () => {
  try {
    await axios.get("http://radio-server:8080/health", { timeout: 3000 });
    if (!radioServerAvailable) {
      radioServerAvailable = true;
      console.log("Radio server is back online - notifying clients");
      io.emit("server-status", { available: true });
      // Also trigger a config refresh
      try {
        const configResponse = await axios.get(
          "http://radio-server:8080/api/config",
          { timeout: 5000 },
        );
        const audioStreamUrl =
          configResponse.data?.station?.audio_stream?.playlist ||
          RADIO_STREAM_URL;
        io.emit("config", {
          station: configResponse.data.station,
          schedule: configResponse.data.schedule,
          stream_url: audioStreamUrl,
          video_stream_url: normalizeVideoPath(
            configResponse.data?.station?.video_stream?.playlist,
          ),
        });
      } catch (e) {
        console.log("Error fetching config after reconnect:", e.message);
      }
    }
  } catch (error) {
    if (radioServerAvailable) {
      radioServerAvailable = false;
      console.log("Radio server health check failed - notifying clients");
      io.emit("server-status", { available: false });
    }
  }
}, 5000); // Check every 5 seconds

// Start server
server.listen(PORT, () => {
  console.log(`Radio web interface running on port ${PORT}`);
  console.log(`Stream URL: ${RADIO_STREAM_URL}`);
});
