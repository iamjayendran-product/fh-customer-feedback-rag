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
  function ensureToken(){
    return fetch("/api/config").then(function(r){ return r.json(); }).then(function(cfg){
      if(cfg.auth_required && !getToken()){
        var t = window.prompt("Enter the access code for Feedback Radar:") || "";
        setToken(t.trim());
      }
    }).catch(function(){ /* no /api/config (older server) - proceed unauthenticated */ });
  }

  var thread = document.getElementById("thread");
  var emptyState = document.getElementById("empty-state");
  var input = document.getElementById("input");
  var sendBtn = document.getElementById("send");
  var startersEl = document.getElementById("starters");

  var history = []; // [{role:'user'|'assistant', content}]
  var busy = false;

  function loadHistory(){
    try{
      var raw = localStorage.getItem(HISTORY_KEY);
      return raw ? JSON.parse(raw) : [];
    }catch(e){ return []; }
  }
  function saveHistory(){
    try{ localStorage.setItem(HISTORY_KEY, JSON.stringify(history)); }catch(e){}
  }

  STARTERS.forEach(function(s){
    var b = document.createElement("button");
    b.className = "chip";
    b.textContent = s;
    b.onclick = function(){ input.value = s; handleSend(); };
    startersEl.appendChild(b);
  });

  function addBubble(role, text){
    var wrap = document.createElement("div");
    wrap.className = "msg " + (role === "user" ? "user" : "ai");
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    wrap.appendChild(bubble);
    thread.appendChild(wrap);
    thread.scrollTop = thread.scrollHeight;
    return { wrap: wrap, bubble: bubble };
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
      var tag = document.createElement("span");
      tag.className = "sentiment-tag " + (r.overall_sentiment || "neutral");
      tag.textContent = r.overall_sentiment || "?";
      var id = document.createElement("span");
      id.className = "source-id";
      id.textContent = r.review_id + " · " + (r.rating != null ? r.rating + "★" : "-");
      var quote = document.createElement("span");
      quote.className = "source-quote";
      quote.textContent = r.text;
      row.appendChild(tag);
      row.appendChild(id);
      row.appendChild(quote);
      details.appendChild(row);
    });
    wrap.appendChild(details);
  }

  function replayHistory(){
    history.forEach(function(turn){
      var b = addBubble(turn.role, turn.content);
      if(turn.role === "assistant" && turn.sources) renderSources(b.wrap, turn.sources);
    });
  }

  function handleSend(){
    if(busy) return;
    var text = input.value.trim();
    if(!text) return;
    if(emptyState){ emptyState.remove(); emptyState = null; }

    addBubble("user", text);
    var priorHistory = history.slice();
    history.push({ role:"user", content:text });
    saveHistory();
    input.value = "";
    input.style.height = "auto";

    busy = true;
    sendBtn.disabled = true;
    var ai = addBubble("ai", "Thinking...");
    ai.bubble.classList.add("thinking");
    var streamedAny = false;

    function finish(){
      busy = false;
      sendBtn.disabled = false;
      input.focus();
      thread.scrollTop = thread.scrollHeight;
    }

    fetch("/api/chat", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
      body: JSON.stringify({ message: text, history: priorHistory })
    }).then(function(r){
      if(r.status === 401){
        setToken(""); // wrong/stale token - clear it so the next question re-prompts
        throw new Error("Access code rejected - refresh the page and re-enter it.");
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
              // Real evidence, on screen well under a second - before the
              // summary has even started generating.
              renderSources(ai.wrap, event.sources);
              thread.scrollTop = thread.scrollHeight;
            } else if(event.type === "delta"){
              if(!streamedAny){
                ai.bubble.classList.remove("thinking");
                ai.bubble.textContent = "";
                streamedAny = true;
              }
              ai.bubble.textContent += event.text;
              thread.scrollTop = thread.scrollHeight;
            } else if(event.type === "done"){
              ai.bubble.classList.remove("thinking");
              ai.bubble.textContent = event.answer;
              // sources were already rendered from the "sources" event above;
              // a cache hit skips straight here with no deltas in between.
              history.push({ role:"assistant", content: event.answer, sources: event.sources });
              saveHistory();
            }
          });
          return pump();
        });
      }
      return pump();
    }).catch(function(e){
      ai.bubble.classList.remove("thinking");
      ai.bubble.textContent = "Couldn't reach the local server (" + (e && e.message ? e.message : "unknown error") + ") - is it still running?";
      history.pop();
      saveHistory();
    }).finally(finish);
  }

  sendBtn.addEventListener("click", handleSend);
  input.addEventListener("keydown", function(e){
    if(e.key === "Enter" && !e.shiftKey){ e.preventDefault(); handleSend(); }
  });
  input.addEventListener("input", function(){
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  });

  history = loadHistory();
  if(history.length){
    if(emptyState){ emptyState.remove(); emptyState = null; }
    replayHistory();
  }
  ensureToken().then(function(){ input.focus(); });
})();
