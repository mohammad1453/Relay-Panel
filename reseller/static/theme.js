'use strict';
// Runs before the page paints so the saved theme never flashes.
(function(){let theme='dark';try{theme=localStorage.getItem('relay-theme')||(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark');}catch(e){}document.documentElement.dataset.theme=theme;})();
