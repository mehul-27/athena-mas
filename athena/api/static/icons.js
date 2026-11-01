/* Athena's local Circum icon helper. The sprite is served from this origin. */
(function (root) {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const SPRITE = "/assets/circum/icons.svg";

  function icon(name, className) {
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", className || "icon");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const use = document.createElementNS(NS, "use");
    use.setAttribute("href", SPRITE + "#i-" + name);
    svg.appendChild(use);
    return svg;
  }

  root.AthenaIcons = { icon: icon, sprite: SPRITE };
}(typeof globalThis !== "undefined" ? globalThis : this));
