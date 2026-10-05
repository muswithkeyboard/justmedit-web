(function () {
  "use strict";

  var ROLE_RU = { patient: "Пациент", doctor: "Врач" };

  function fetchMe() {
    if (document.body.getAttribute("data-session") !== "1") { return Promise.resolve(null); }
    return fetch("/api/v1/me", { credentials: "same-origin", cache: "no-store", headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (u) { return u && u.login && u.role ? u : null; })
      .catch(function () { return null; });
  }

  function show(user) {
    var link = document.getElementById("nav-cabinet");
    if (!link) { return; }
    while (link.firstChild) { link.removeChild(link.firstChild); }
    if (!user) {
      link.setAttribute("href", "/login");
      link.appendChild(document.createTextNode("Войти"));
      link.classList.remove("nav__cabinet--in");
      link.removeAttribute("title");
      link.removeAttribute("aria-label");
      return;
    }
    var role = ROLE_RU[user.role] || "";
    link.setAttribute("href", "/cabinet");
    link.classList.add("nav__cabinet--in");
    var dot = document.createElement("span");
    dot.className = "nav__dot";
    dot.setAttribute("aria-hidden", "true");
    var name = document.createElement("span");
    name.className = "nav__user";
    name.textContent = user.login;
    var badge = document.createElement("span");
    badge.className = "nav__role";
    badge.textContent = role;
    link.appendChild(dot);
    link.appendChild(name);
    link.appendChild(badge);
    link.title = "Кабинет: " + user.login + (role ? " · " + role.toLowerCase() : "");

    link.setAttribute("aria-label", "Кабинет: " + user.login + (role ? ", " + role.toLowerCase() : ""));
  }

  var me = fetchMe();
  me.then(show);
  window.JMNav = {
    me: me,

    set: function (user) { window.JMNav.me = Promise.resolve(user || null); show(user || null); },

    refresh: function () {
      document.body.setAttribute("data-session", "1");
      window.JMNav.me = fetchMe();
      return window.JMNav.me.then(function (u) { show(u); return u; });
    }
  };
})();
