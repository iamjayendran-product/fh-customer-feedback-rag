(function(){
  "use strict";

  var STARTERS = [
    "What are people saying about refunds?",
    "Is app reliability getting better or worse?",
    "What do diners love most about Foodhub?",
    "Any evidence of restaurant fraud or scams?",
    "What's the single biggest problem right now?"
  ];

  var HISTORY_KEY = "fh_chat_history_v1";
  var TOKEN_KEY = "fh_access_token_v1";
  var THEME_KEY = "fh_theme_v1";

  function getStoredTheme(){
    try{ return localStorage.getItem(THEME_KEY); }catch(e){ return null; }
  }
  function storeTheme(theme){
    try{ localStorage.setItem(THEME_KEY, theme); }catch(e){}
  }
  function applyTheme(theme){
    document.documentElement.setAttribute("data-theme", theme);
    var toggle = document.getElementById("theme-toggle");
    if(toggle) toggle.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
  }

  function getToken(){
    try{ return localStorage.getItem(TOKEN_KEY) || ""; }catch(e){ return ""; }
  }
  function setToken(t){
    try{ localStorage.setItem(TOKEN_KEY, t); }catch(e){}
  }
  function authHeaders(){
    var t = getToken();
    return t ? { "Authorization": "Bearer " + t } : {};
  }
  function promptForToken(){
    var t = window.prompt("Enter the access code for FH ReviewIQ:") || "";
    setToken(t.trim());
  }
  function ensureToken(){
    return fetch("/api/config").then(function(r){ return r.json(); }).then(function(cfg){
      if(cfg.auth_required && !getToken()) promptForToken();
    }).catch(function(){ /* no /api/config (older server) - proceed unauthenticated */ });
  }

  var thread = document.getElementById("thread");
  var emptyState = document.getElementById("empty-state");
  var input = document.getElementById("input");
  var sendBtn = document.getElementById("send");
  var startersEl = document.getElementById("starters");
  var faqPopover = document.getElementById("faq-popover");

  var history = []; // [{role:'user'|'assistant', content}]
  var busy = false;

  // Runs after layout/paint (rAF) rather than immediately, so scrollHeight
  // always reflects the bubble's real, settled size - calling this right
  // after a DOM mutation without rAF is what let the loading bubble's
  // height grow (a longer rotating phrase wrapping to a 2nd line) without
  // the view re-scrolling to match, leaving it tucked under the composer.
  function scrollToBottom(smooth){
    requestAnimationFrame(function(){
      // "auto" would defer to the CSS scroll-behavior:smooth set on the
      // thread, animating even the frequent per-delta calls below - explicit
      // "instant" is what actually forces a non-animated jump regardless.
      thread.scrollTo({ top: thread.scrollHeight, behavior: smooth ? "smooth" : "instant" });
    });
  }

  function loadHistory(){
    try{
      var raw = localStorage.getItem(HISTORY_KEY);
      return raw ? JSON.parse(raw) : [];
    }catch(e){ return []; }
  }
  function saveHistory(){
    try{ localStorage.setItem(HISTORY_KEY, JSON.stringify(history)); }catch(e){}
  }

  // mousedown preventDefault + act on click (rather than a plain onclick)
  // stops the button's default mousedown behaviour from shifting focus
  // away from #input first - without this, a genuine mouse/trackpad click
  // fires blur-then-click, and on some browsers the resulting focus churn
  // eats the very first click on a freshly-rendered bubble. This makes a
  // press register every time, first click included.
  function makeClickable(el, onActivate){
    el.addEventListener("mousedown", function(e){ e.preventDefault(); });
    el.addEventListener("click", onActivate);
  }

  STARTERS.forEach(function(s){
    var b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.textContent = s;
    makeClickable(b, function(){ input.value = s; handleSend(); });
    startersEl.appendChild(b);
  });

  // Same starter questions, re-shown as a small popover of "frequently
  // asked" bubbles whenever the composer is tapped - a shortcut back to
  // them once the empty-state chips have scrolled out of view.
  STARTERS.forEach(function(s, i){
    var b = document.createElement("button");
    b.type = "button";
    b.className = "faq-chip";
    b.style.transitionDelay = (i * 30) + "ms";
    b.textContent = s;
    makeClickable(b, function(){ input.value = s; hideFaqPopover(); handleSend(); });
    faqPopover.appendChild(b);
  });
  function showFaqPopover(){
    if(!input.value.trim()) faqPopover.classList.add("open");
  }
  function hideFaqPopover(){
    faqPopover.classList.remove("open");
  }

  function addBubble(role, text){
    var wrap = document.createElement("div");
    wrap.className = "msg " + (role === "user" ? "user" : "ai");
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    wrap.appendChild(bubble);
    thread.appendChild(wrap);
    scrollToBottom(true);
    return { wrap: wrap, bubble: bubble };
  }

  // Minimal markdown renderer for the AI bubble - just enough for a short
  // answer with an occasional **bold** term or a small comparison table
  // (see SYSTEM_PROMPT in rag.py for what the model is told it may use).
  // Escapes HTML first so nothing in the model's text (which can echo
  // review snippets) is ever interpreted as markup.
  function escapeHtml(s){
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function isTableRow(line){ return /^\s*\|.*\|\s*$/.test(line); }
  function isTableSeparator(line){ return /^\s*\|[\s:\-|]+\|\s*$/.test(line); }
  function isListItem(line){ return /^\s*[-*]\s+/.test(line); }
  function tableCells(line){
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(function(c){ return c.trim(); });
  }
  function renderTable(lines){
    var rows = lines.filter(function(l){ return !isTableSeparator(l); }).map(tableCells);
    if(!rows.length) return "";
    var head = rows[0], body = rows.slice(1);
    var thead = "<thead><tr>" + head.map(function(c){ return "<th>" + c + "</th>"; }).join("") + "</tr></thead>";
    var tbody = "<tbody>" + body.map(function(r){
      return "<tr>" + r.map(function(c){ return "<td>" + c + "</td>"; }).join("") + "</tr>";
    }).join("") + "</tbody>";
    return "<table>" + thead + tbody + "</table>";
  }
  function mdToHtml(raw){
    var text = escapeHtml(raw).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    var lines = text.split("\n");
    var out = [];
    var i = 0;
    while(i < lines.length){
      var line = lines[i];
      if(line.trim() === ""){ i++; continue; }
      if(isTableRow(line)){
        var tableLines = [];
        while(i < lines.length && isTableRow(lines[i])){ tableLines.push(lines[i]); i++; }
        out.push(renderTable(tableLines));
        continue;
      }
      if(isListItem(line)){
        var items = [];
        while(i < lines.length && isListItem(lines[i])){
          items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
          i++;
        }
        out.push("<ul>" + items.map(function(it){ return "<li>" + it + "</li>"; }).join("") + "</ul>");
        continue;
      }
      var para = [line];
      i++;
      while(i < lines.length && lines[i].trim() !== "" && !isTableRow(lines[i]) && !isListItem(lines[i])){
        para.push(lines[i]);
        i++;
      }
      out.push("<p>" + para.join("<br>") + "</p>");
    }
    return '<div class="md">' + out.join("") + "</div>";
  }

  var SOURCE_LABELS = { app_store: "App Store", google_play: "Google Play" };

  function starString(rating){
    if(rating == null) return "";
    var n = Math.max(0, Math.min(5, Math.round(rating)));
    return "★".repeat(n) + "☆".repeat(5 - n);
  }
  function shortDate(iso){
    if(!iso) return "";
    var d = new Date(iso);
    if(isNaN(d)) return "";
    return d.toLocaleDateString(undefined, { month: "short", year: "numeric" });
  }

  function renderSources(wrap, sources){
    if(!sources || !sources.length) return;
    var details = document.createElement("details");
    details.className = "sources";
    var summary = document.createElement("summary");
    summary.textContent = "Sources (" + sources.length + ")";
    details.appendChild(summary);
    sources.forEach(function(r){
      var row = document.createElement("div");
      row.className = "source-row";

      var meta = document.createElement("div");
      meta.className = "source-meta";

      // Deliberately no review_id / native App Store or Google Play ID here -
      // it's an internal dedupe key (see CLAUDE.md conventions), not
      // something a reader needs to see.
      var badge = document.createElement("span");
      badge.className = "source-badge";
      badge.textContent = SOURCE_LABELS[r.source] || r.source || "Review";
      meta.appendChild(badge);

      var stars = starString(r.rating);
      if(stars){
        var rating = document.createElement("span");
        rating.className = "source-rating";
        rating.textContent = stars;
        meta.appendChild(rating);
      }

      var tag = document.createElement("span");
      tag.className = "sentiment-tag " + (r.overall_sentiment || "neutral");
      tag.textContent = r.overall_sentiment || "neutral";
      meta.appendChild(tag);

      var date = shortDate(r.created_at);
      if(date){
        var dateEl = document.createElement("span");
        dateEl.className = "source-date";
        dateEl.textContent = date;
        meta.appendChild(dateEl);
      }

      var quote = document.createElement("div");
      quote.className = "source-quote";
      quote.textContent = "“" + r.text + "”";

      row.appendChild(meta);
      row.appendChild(quote);
      details.appendChild(row);
    });
    wrap.appendChild(details);
  }

  function replayHistory(){
    history.forEach(function(turn){
      var b = addBubble(turn.role, turn.content);
      if(turn.role === "assistant"){
        b.bubble.innerHTML = mdToHtml(turn.content);
        if(turn.sources) renderSources(b.wrap, turn.sources);
      }
    });
  }

  function handleSend(){
    if(busy) return;
    var text = input.value.trim();
    if(!text) return;
    if(emptyState){ emptyState.remove(); emptyState = null; }
    hideFaqPopover();

    addBubble("user", text);
    var priorHistory = history.slice();
    history.push({ role:"user", content:text });
    saveHistory();
    input.value = "";
    input.style.height = "auto";

    busy = true;
    sendBtn.disabled = true;
    var ai = addBubble("ai", "");
    ai.bubble.classList.add("thinking");
    var streamedAny = false;

    // Lottie loading animation (img/loading.json, played by the self-hosted
    // lottie-web) beside rotating status phrases.
    var LOADING_PHRASES = [
      "Looking it up",
      "Checking the records\u2026",
      "Finding relevant information\u2026",
      "Connecting the dots...",
      "Retrieving insights\u2026",
      "Building your answer\u2026",
      "Retrieving relevant data\u2026"
    ];
    ai.bubble.innerHTML = '<span class="loader"><span class="loader-anim"></span>'
      + '<span class="loading-text"></span></span>';
    var textEl = ai.bubble.querySelector(".loading-text");
    var lottieAnim = null;
    if(window.lottie){
      try{
        lottieAnim = window.lottie.loadAnimation({
          container: ai.bubble.querySelector(".loader-anim"),
          renderer: "svg", loop: true, autoplay: true, path: "/img/loading.json"
        });
      }catch(e){ lottieAnim = null; }
    }
    var loadingIndex = 0;
    function showLoadingPhrase(){
      textEl.classList.add("fade-out");
      setTimeout(function(){
        textEl.textContent = LOADING_PHRASES[loadingIndex++ % LOADING_PHRASES.length];
        textEl.classList.remove("fade-out");
        scrollToBottom(false);
      }, textEl.textContent ? 160 : 0);
    }
    showLoadingPhrase();
    var loadingTimer = setInterval(showLoadingPhrase, 1800);
    function stopLoadingPhrases(){
      clearInterval(loadingTimer);
      if(lottieAnim){ lottieAnim.destroy(); lottieAnim = null; }
    }

    function finish(){
      stopLoadingPhrases();
      busy = false;
      sendBtn.disabled = false;
      input.focus();
      scrollToBottom(true);
    }

    // isRetry guards against looping forever if the code is wrong twice in
    // a row - one automatic re-prompt, then it's a real error.
    function attemptChat(isRetry){
      return fetch("/api/chat", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
      body: JSON.stringify({ message: text, history: priorHistory })
    }).then(function(r){
      if(r.status === 401){
        setToken(""); // wrong/stale token
        if(isRetry) throw new Error("Access code rejected - check it and try again.");
        // Ask right here, no page reload needed - this is what used to be
        // missing: ensureToken() only ever ran once, at page load, so a
        // bad/expired token had no way to get corrected short of a refresh.
        promptForToken();
        return attemptChat(true);
      }
      if(!r.ok || !r.body){
        return r.json().catch(function(){ return {}; }).then(function(data){
          throw new Error(data.error || ("HTTP " + r.status));
        });
      }
      var reader = r.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";

      function pump(){
        return reader.read().then(function(res){
          if(res.done) return;
          buffer += decoder.decode(res.value, { stream: true });
          var lines = buffer.split("\n");
          buffer = lines.pop(); // last, possibly incomplete line stays in buffer
          lines.forEach(function(line){
            line = line.trim();
            if(!line) return;
            var event;
            try{ event = JSON.parse(line); }catch(e){ return; }

            if(event.type === "sources"){
              // Retrieval finishes well under a second, long before the
              // summary is done generating - but Sources only gets shown
              // once the answer itself has fully arrived (see "done"
              // below), so there's nothing to render yet on this event.
            } else if(event.type === "delta"){
              if(!streamedAny){
                stopLoadingPhrases();
                ai.bubble.classList.remove("thinking");
                ai.bubble.textContent = "";
                streamedAny = true;
              }
              ai.bubble.textContent += event.text;
              scrollToBottom(false);
            } else if(event.type === "done"){
              stopLoadingPhrases();
              ai.bubble.classList.remove("thinking");
              ai.bubble.innerHTML = mdToHtml(event.answer);
              renderSources(ai.wrap, event.sources);
              scrollToBottom(true);
              history.push({ role:"assistant", content: event.answer, sources: event.sources });
              saveHistory();
            }
          });
          return pump();
        });
      }
      return pump();
    });
    }

    attemptChat(false).catch(function(e){
      ai.bubble.classList.remove("thinking");
      var msg = e && e.message ? e.message : "unknown error";
      ai.bubble.textContent = /access code/i.test(msg)
        ? msg
        : "Couldn't reach the server (" + msg + ") - is it still running?";
      history.pop();
      saveHistory();
    }).finally(finish);
  }

  var currentTheme = getStoredTheme() || "dark";
  applyTheme(currentTheme);
  var themeToggle = document.getElementById("theme-toggle");
  if(themeToggle){
    themeToggle.addEventListener("click", function(){
      currentTheme = currentTheme === "dark" ? "light" : "dark";
      applyTheme(currentTheme);
      storeTheme(currentTheme);
    });
  }

  sendBtn.addEventListener("click", handleSend);
  input.addEventListener("keydown", function(e){
    if(e.key === "Enter" && !e.shiftKey){ e.preventDefault(); handleSend(); }
  });
  input.addEventListener("input", function(){
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
    if(input.value.trim()) hideFaqPopover();
  });
  // "click" rather than "focus" - the composer is re-focused programmatically
  // after every reply (see finish() above) and on page load, neither of
  // which is the user actually tapping in to ask something new.
  input.addEventListener("click", showFaqPopover);
  // Standard click-outside-to-close, rather than hiding on input "blur" -
  // blur fires the instant a faq-chip is pressed (focus moves to the
  // button), which raced against the click that's supposed to fire right
  // after it. Closing on outside-click instead means a click ON a chip is
  // never in a race with the thing that's supposed to close the popover.
  document.addEventListener("click", function(e){
    if(!faqPopover.contains(e.target) && e.target !== input) hideFaqPopover();
  });

  history = loadHistory();
  if(history.length){
    if(emptyState){ emptyState.remove(); emptyState = null; }
    replayHistory();
  }
  ensureToken().then(function(){ input.focus(); });
})();
