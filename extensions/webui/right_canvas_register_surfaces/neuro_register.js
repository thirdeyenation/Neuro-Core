// Neuro Core — surface registration at framework init time.
// Host contract (see /a0/extensions/webui/AGENTS.md and /a0/webui/js/extensions.js):
// callJsExtensions invokes this module's default export with the rightCanvasStore
// instance as argument during rightCanvasStore.init(), before any user interaction.
// registerSurface() is an idempotent upsert, so the sidebar quick-action entry
// (extensions/webui/sidebar-quick-actions-main-start/neuro-entry.html) remains a
// harmless defensive fallback.
export default async function registerNeuroCoreSurface(store) {
  store.registerSurface({
    id: 'neuro-core-graph',
    title: 'Neuro Core',
    icon: 'hub',
    order: 50,
    canOpen: function() { return true; },
    open: async function() {},
    close: async function() {}
  });
}
