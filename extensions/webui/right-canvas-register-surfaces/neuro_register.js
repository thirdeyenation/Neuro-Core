// Neuro Core — surface registration at framework init time.
// Called by right-canvas-store.js via callJsExtensions("right_canvas_register_surfaces", this)
// where `this` is the rightCanvasStore instance. registerSurface() is idempotent (upsert).
this.registerSurface({
  id: 'neuro-core-graph',
  title: 'Neuro Core',
  icon: 'hub',
  order: 50,
  canOpen: function() { return true; },
  open: async function() {},
  close: async function() {}
});
