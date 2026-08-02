// Neuro Core — Alpine.js context graph store
//
// Exposes ``Alpine.store('neuroGraph', ...)`` with reactive state for
// the WebUI graph panel: nodes, edges, query, loading, error.
//
// Conventions (per Agent Zero WebUI guidelines):
//   * Use createStore from /js/AlpineStore.js
//   * Use the notification system (toastFrontendError/Success) rather
//     than inline error boxes
//   * Fetch from ``GET /api/plugins/neuro_core/context_graph``
//   * No external dependencies beyond Alpine.js
//
// The panel HTML gates on ``$store.neuroGraph`` so the store is only
// mounted when the panel is open. See ``graph-panel.html``.

import { createStore } from '/js/AlpineStore.js';
import {
  toastFrontendError,
  toastFrontendSuccess,
} from '/components/notifications/notification-store.js';

const API_BASE = '/api/plugins/neuro_core';

export const neuroGraphStore = createStore('neuroGraph', {
  // Reactive state --------------------------------------------------
  nodes: [],
  edges: [],
  query: '',
  memory_subdir: 'default',
  loading: false,
  error: null,
  last_fetched_at: null,

  // Advanced Filters state ----------------------------------------
  advancedFilters: {
    memory_types: [],
    validation_statuses: [],
    relationship_types: [],
    date_range_start: '',
    date_range_end: '',
    importance_min: 0.0,
    confidence_min: 0.0,
    stability_min: 0.0,
    episode_id: '',
  },
  filterPanelOpen: false,
  filterResults: null,
  filterLoading: false,

  // Computed-ish helpers -------------------------------------------
  get isEmpty() {
    return !this.loading && !this.error && this.nodes.length === 0;
  },

  get hasResults() {
    return this.nodes.length > 0;
  },

  // Methods ---------------------------------------------------------

  /**
   * Fetch the context graph for a query and memory subdir.
   *
   * Updates ``loading`` and ``error`` reactively. On success
   * populates ``nodes`` and ``edges``; on failure surfaces a toast
   * and sets ``error``.
   *
   * @param {string} query
   * @param {string} memory_subdir
   * @returns {Promise<boolean>} true on success
   */
  async fetch(query, memory_subdir) {
    const q = (query || '').trim();
    const sub = (memory_subdir || 'default').trim() || 'default';

    if (!q) {
      this.error = 'Query is required';
      toastFrontendError('Query is required', 'Neuro Core');
      return false;
    }

    this.loading = true;
    this.error = null;
    this.query = q;
    this.memory_subdir = sub;

    const url = new URL(API_BASE + '/context_graph', window.location.origin);
    url.searchParams.set('query', q);
    url.searchParams.set('memory_subdir', sub);

    try {
      const resp = await fetch(url.toString(), {
        method: 'GET',
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
      });

      if (!resp.ok) {
        const text = await resp.text().catch(() => '');
        throw new Error(
          `HTTP ${resp.status} ${resp.statusText}${text ? ': ' + text : ''}`
        );
      }

      const data = await resp.json();
      this.nodes = Array.isArray(data?.nodes) ? data.nodes : [];
      this.edges = Array.isArray(data?.edges) ? data.edges : [];
      this.last_fetched_at = new Date().toISOString();

      toastFrontendSuccess(
        `Loaded ${this.nodes.length} node(s) for "${q}"`,
        'Neuro Core'
      );
      return true;
    } catch (err) {
      this.error = (err && err.message) || String(err);
      this.nodes = [];
      this.edges = [];
      toastFrontendError(this.error, 'Neuro Core');
      return false;
    } finally {
      this.loading = false;
    }
  },

  /**
   * Reset all reactive state.
   */
  clear() {
    this.nodes = [];
    this.edges = [];
    this.query = '';
    this.error = null;
    this.loading = false;
    this.last_fetched_at = null;
  },

  /**
   * Cleanup hook called by the panel's x-destroy directive.
   */
  cleanup() {
    // We intentionally keep nodes/edges cached so the panel can be
    // re-opened without a re-fetch. Use ``clear()`` for a hard reset.
  },

  /**
   * On-mount hook called by the panel's x-init directive.
   */
  onOpen() {
    // No-op for now; the panel is read-only until ``fetch`` is called.
  },
});

  // Advanced Filters methods ------------------------------------
  toggleFilterPanel() {
    this.filterPanelOpen = !this.filterPanelOpen;
  },
  async applyAdvancedFilters() {
    this.filterLoading = true;
    this.error = null;
    try {
      const params = new URLSearchParams();
      params.set('memory_subdir', this.memory_subdir || 'default');
      if (this.advancedFilters.memory_types.length > 0) {
        params.set('memory_type', this.advancedFilters.memory_types.join(','));
      }
      if (this.advancedFilters.validation_statuses.length > 0) {
        params.set('validation_status', this.advancedFilters.validation_statuses.join(','));
      }
      if (this.advancedFilters.relationship_types.length > 0) {
        params.set('relationship_type', this.advancedFilters.relationship_types.join(','));
      }
      if (this.advancedFilters.date_range_start) {
        params.set('date_range_start', this.advancedFilters.date_range_start);
      }
      if (this.advancedFilters.date_range_end) {
        params.set('date_range_end', this.advancedFilters.date_range_end);
      }
      if (this.advancedFilters.importance_min > 0) {
        params.set('importance_min', this.advancedFilters.importance_min);
      }
      if (this.advancedFilters.confidence_min > 0) {
        params.set('confidence_min', this.advancedFilters.confidence_min);
      }
      if (this.advancedFilters.stability_min > 0) {
        params.set('stability_min', this.advancedFilters.stability_min);
      }
      if (this.advancedFilters.episode_id) {
        params.set('episode_id', this.advancedFilters.episode_id);
      }
      const url = API_BASE + '/advanced_filters?' + params.toString();
      const resp = await fetch(url, { credentials: 'same-origin' });
      const data = await resp.json();
      if (data.success) {
        this.filterResults = data;
        this.nodes = Array.isArray(data.nodes) ? data.nodes : [];
        this.edges = Array.isArray(data.edges) ? data.edges : [];
        toastFrontendSuccess('Filters applied', 'Neuro Core');
      } else {
        this.error = data.error || 'Filter request failed';
        toastFrontendError(this.error, 'Neuro Core');
      }
    } catch (err) {
      this.error = (err && err.message) || String(err);
      toastFrontendError(this.error, 'Neuro Core');
    } finally {
      this.filterLoading = false;
    }
  },
  clearAdvancedFilters() {
    this.advancedFilters = {
      memory_types: [],
      validation_statuses: [],
      relationship_types: [],
      date_range_start: '',
      date_range_end: '',
      importance_min: 0.0,
      confidence_min: 0.0,
      stability_min: 0.0,
      episode_id: '',
    };
    this.filterResults = null;
  },
  get activeFilterCount() {
    const f = this.advancedFilters || {};
    return (f.memory_types?.length || 0) +
           (f.validation_statuses?.length || 0) +
           (f.relationship_types?.length || 0) +
           (f.date_range_start ? 1 : 0) +
           (f.date_range_end ? 1 : 0) +
           (f.importance_min > 0 ? 1 : 0) +
           (f.confidence_min > 0 ? 1 : 0) +
           (f.stability_min > 0 ? 1 : 0) +
           (f.episode_id ? 1 : 0);
  },

// Register the store with Alpine on first import. Alpine will pick
// this up via the global ``Alpine`` handle. If Alpine is not ready
// yet (script-load order), we listen for ``alpine:init``.
if (typeof window !== 'undefined') {
  const register = () => {
    if (window.Alpine && typeof window.Alpine.store === 'function') {
      window.Alpine.store('neuroGraph', neuroGraphStore);
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', register, { once: true });
  } else {
    register();
  }

  // Also try on alpine:init (some Agent Zero mounts use this event).
  document.addEventListener('alpine:init', register, { once: true });
}
