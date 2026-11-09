/* The window's page side (kicad-plugin-site-design 5.3). Every element with a
   data-action posts it, with its data-* fields, to the plugin through the web
   view's script message handler; the plugin answers by replacing regions with
   window.rftoolsUpdate({region: html}). No logic lives here: webui.handle
   decides everything. Fields post as they change and are never re-rendered
   while typed in. */
(function () {
  "use strict";

  function handler() {
    if (window.rftools && window.rftools.postMessage) return window.rftools;
    var webkit = window.webkit && window.webkit.messageHandlers;
    if (webkit && webkit.rftools) return webkit.rftools;
    if (window.chrome && window.chrome.webview) return window.chrome.webview;
    return null;
  }

  function post(message) {
    var h = handler();
    if (h) h.postMessage(JSON.stringify(message));
  }

  function fields(el) {
    var message = {};
    for (var key in el.dataset) message[key] = el.dataset[key];
    return message;
  }

  function fieldMessage(el) {
    var message = fields(el);
    if (message.action === "form") {
      if (message.field === "extra_layers") {
        message.value = [];
        var boxes = document.querySelectorAll('input[data-action="form"][data-field="extra_layers"]');
        for (var i = 0; i < boxes.length; i++) {
          if (boxes[i].dataset.name === el.dataset.name && boxes[i].checked) message.value.push(boxes[i].value);
        }
      } else if (el.type === "checkbox") {
        message.value = el.checked;
      } else {
        message.value = el.value;
      }
    } else if (message.action === "mask") {
      message.on = el.checked;
    } else if (message.action === "grid") {
      message.text = el.value;
    }
    return message;
  }

  document.addEventListener("click", function (event) {
    var el = event.target.closest ? event.target.closest("[data-action]") : null;
    if (!el || el.disabled || el.tagName === "INPUT" || el.tagName === "SELECT") return;
    event.preventDefault();
    var message = fields(el);
    if (message.action === "save_key") {
      var field = document.getElementById("key-field");
      message.text = field ? field.value : "";
      if (field) field.value = "";
    }
    post(message);
  });

  document.addEventListener("input", function (event) {
    var el = event.target;
    if (el.matches && el.matches('input[type="text"][data-action]')) post(fieldMessage(el));
  });

  document.addEventListener("change", function (event) {
    var el = event.target;
    if (el.matches && el.matches('input[type="checkbox"][data-action], select[data-action]')) {
      post(fieldMessage(el));
    }
  });

  // The page is the window: a reload or Back would leave it blank (the plugin
  // also refuses any navigation once the page is ready).
  function leavesThePage(event) {
    var key = event.key;
    if (key === "F5" || key === "BrowserBack" || key === "BrowserForward" || key === "BrowserRefresh") return true;
    if ((event.ctrlKey || event.metaKey) && (key === "r" || key === "R")) return true;
    return event.altKey && (key === "ArrowLeft" || key === "ArrowRight");
  }

  document.addEventListener("keydown", function (event) {
    if (leavesThePage(event)) {
      event.preventDefault();
      return;
    }
    if (event.key === "Enter" && event.target.id === "key-field") {
      var save = document.querySelector('[data-action="save_key"]');
      if (save) save.click();
    }
  });

  // A mouse's back and forward buttons (3 and 4).
  document.addEventListener("mouseup", function (event) {
    if (event.button === 3 || event.button === 4) event.preventDefault();
  });

  window.rftoolsUpdate = function (regions) {
    for (var name in regions) {
      var node = document.getElementById("r-" + name);
      if (node) node.innerHTML = regions[name];
    }
  };

  // Links in the page never navigate it: the plugin opens them in the browser.
  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("a[href]") : null;
    if (link) {
      event.preventDefault();
      post({ action: "open", href: link.getAttribute("href") });
    }
  }, true);

  if (document.readyState === "complete") post({ action: "ready" });
  else window.addEventListener("load", function () { post({ action: "ready" }); });
})();
