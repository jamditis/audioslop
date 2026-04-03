(function () {
  "use strict";

  // DOM refs
  var audio = document.getElementById("audio");
  var seekBar = document.getElementById("seek-bar");
  var timeCurrent = document.getElementById("time-current");
  var timeTotal = document.getElementById("time-total");
  var btnPlay = document.getElementById("btn-play");
  var iconPlay = document.getElementById("icon-play");
  var iconPause = document.getElementById("icon-pause");
  var btnBack = document.getElementById("btn-back");
  var btnForward = document.getElementById("btn-forward");
  var speedSelect = document.getElementById("speed-select");
  var volumeSlider = document.getElementById("volume-slider");
  var transcriptEl = document.getElementById("transcript");

  // State
  var timeline = [];
  var currentIdx = -1;
  var isUserSeeking = false;

  // -----------------------------------------------------------------------
  // Time formatting
  // -----------------------------------------------------------------------

  function fmt(seconds) {
    if (!seconds || !isFinite(seconds)) return "0:00";
    var s = Math.floor(seconds);
    var m = Math.floor(s / 60);
    var ss = s % 60;
    return m + ":" + (ss < 10 ? "0" : "") + ss;
  }

  // -----------------------------------------------------------------------
  // Transcript loading
  // -----------------------------------------------------------------------

  function loadTranscript() {
    fetch("/api/job/" + JOB_ID + "/segments")
      .then(function (res) { return res.json(); })
      .then(function (segments) {
        // Clear loading text
        while (transcriptEl.firstChild) {
          transcriptEl.removeChild(transcriptEl.firstChild);
        }

        var globalOffset = 0;

        segments.forEach(function (seg, segIdx) {
          // Divider between segments (not before the first)
          if (segIdx > 0) {
            var divider = document.createElement("div");
            divider.className = "segment-divider";
            transcriptEl.appendChild(divider);
          }

          // Container for this segment
          var container = document.createElement("div");
          if (seg.is_title) {
            container.className = "segment-title mb-3";
          } else {
            container.className = "mb-4";
          }

          // Parse word timings
          var words = [];
          if (seg.word_timings_json) {
            try {
              words = JSON.parse(seg.word_timings_json);
            } catch (e) {
              words = [];
            }
          }

          if (words.length > 0) {
            words.forEach(function (w) {
              var span = document.createElement("span");
              span.className = "word";
              span.textContent = w.word + " ";

              var gStart = globalOffset + w.start;
              var gEnd = globalOffset + w.end;
              span.setAttribute("data-start", gStart.toFixed(3));
              span.setAttribute("data-end", gEnd.toFixed(3));

              span.addEventListener("click", function () {
                audio.currentTime = gStart;
                if (audio.paused) {
                  audio.play();
                }
              });

              timeline.push({ globalStart: gStart, globalEnd: gEnd, el: span });
              container.appendChild(span);
            });
          } else {
            container.textContent = seg.source_text || "";
          }

          transcriptEl.appendChild(container);

          // Advance offset past this segment's audio + silence gap
          globalOffset += (seg.duration_seconds || 0) + (seg.pause_after || 0);
        });

        // Sort timeline by start time
        timeline.sort(function (a, b) { return a.globalStart - b.globalStart; });
      })
      .catch(function (err) {
        transcriptEl.textContent = "Failed to load transcript.";
      });
  }

  // -----------------------------------------------------------------------
  // Binary search
  // -----------------------------------------------------------------------

  function binarySearch(time) {
    if (timeline.length === 0) return -1;

    var lo = 0;
    var hi = timeline.length - 1;
    var closest = -1;
    var closestDist = Infinity;

    while (lo <= hi) {
      var mid = (lo + hi) >>> 1;
      var entry = timeline[mid];

      if (time >= entry.globalStart && time <= entry.globalEnd) {
        return mid;
      }

      var dist = Math.abs(entry.globalStart - time);
      if (dist < closestDist) {
        closestDist = dist;
        closest = mid;
      }

      if (time < entry.globalStart) {
        hi = mid - 1;
      } else {
        lo = mid + 1;
      }
    }

    // Return closest only if time is past its start (already spoken)
    if (closest >= 0 && time >= timeline[closest].globalStart) {
      return closest;
    }
    // If time is before the closest word, return the previous one
    if (closest > 0 && time < timeline[closest].globalStart) {
      return closest - 1;
    }
    return closest;
  }

  // -----------------------------------------------------------------------
  // Sync loop
  // -----------------------------------------------------------------------

  function syncLoop() {
    requestAnimationFrame(syncLoop);

    if (audio.paused || timeline.length === 0) return;

    var time = audio.currentTime;
    var idx = binarySearch(time);

    if (idx === currentIdx) return;

    // Remove highlight from previous word
    if (currentIdx >= 0 && currentIdx < timeline.length) {
      timeline[currentIdx].el.classList.remove("word-current");
      timeline[currentIdx].el.classList.add("word-spoken");
    }

    // Mark all words between old and new as spoken
    if (idx > currentIdx) {
      var start = Math.max(0, currentIdx + 1);
      for (var i = start; i < idx; i++) {
        timeline[i].el.classList.remove("word-current");
        timeline[i].el.classList.add("word-spoken");
      }
    }

    // Highlight new current word
    if (idx >= 0 && idx < timeline.length) {
      timeline[idx].el.classList.add("word-current");
      timeline[idx].el.classList.remove("word-spoken");

      // Auto-scroll if current word is outside visible area
      var wordRect = timeline[idx].el.getBoundingClientRect();
      var containerRect = transcriptEl.getBoundingClientRect();
      if (wordRect.top < containerRect.top || wordRect.bottom > containerRect.bottom) {
        timeline[idx].el.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    }

    currentIdx = idx;
  }

  // -----------------------------------------------------------------------
  // Seek handler -- reset highlights on seek
  // -----------------------------------------------------------------------

  audio.addEventListener("seeked", function () {
    var time = audio.currentTime;

    // Reset all highlights
    for (var i = 0; i < timeline.length; i++) {
      timeline[i].el.classList.remove("word-current");
      timeline[i].el.classList.remove("word-spoken");
    }

    // Mark all words before current time as spoken
    for (var j = 0; j < timeline.length; j++) {
      if (timeline[j].globalEnd <= time) {
        timeline[j].el.classList.add("word-spoken");
      } else {
        break;
      }
    }

    // Force re-sync
    currentIdx = -1;
  });

  // -----------------------------------------------------------------------
  // Controls
  // -----------------------------------------------------------------------

  // Play/pause
  btnPlay.addEventListener("click", function () {
    if (audio.paused) {
      audio.play();
    } else {
      audio.pause();
    }
  });

  audio.addEventListener("play", function () {
    iconPlay.classList.add("hidden");
    iconPause.classList.remove("hidden");
  });

  audio.addEventListener("pause", function () {
    iconPause.classList.add("hidden");
    iconPlay.classList.remove("hidden");
  });

  // Skip back/forward
  btnBack.addEventListener("click", function () {
    audio.currentTime = Math.max(0, audio.currentTime - 10);
  });

  btnForward.addEventListener("click", function () {
    audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 10);
  });

  // Speed
  speedSelect.addEventListener("change", function () {
    audio.playbackRate = parseFloat(speedSelect.value);
  });

  // Volume
  volumeSlider.addEventListener("input", function () {
    audio.volume = parseFloat(volumeSlider.value);
  });

  // Seek bar interaction
  seekBar.addEventListener("mousedown", function () {
    isUserSeeking = true;
  });

  seekBar.addEventListener("touchstart", function () {
    isUserSeeking = true;
  });

  seekBar.addEventListener("input", function () {
    timeCurrent.textContent = fmt(parseFloat(seekBar.value));
  });

  seekBar.addEventListener("change", function () {
    audio.currentTime = parseFloat(seekBar.value);
    isUserSeeking = false;
  });

  // Audio metadata loaded
  audio.addEventListener("loadedmetadata", function () {
    seekBar.max = audio.duration;
    timeTotal.textContent = fmt(audio.duration);
  });

  // Audio time update
  audio.addEventListener("timeupdate", function () {
    if (!isUserSeeking) {
      seekBar.value = audio.currentTime;
      timeCurrent.textContent = fmt(audio.currentTime);
    }
  });

  // -----------------------------------------------------------------------
  // Init
  // -----------------------------------------------------------------------

  loadTranscript();
  syncLoop();

})();
