/* On-demand mounted-volume discovery; selection is reviewed before starting existing jobs. */
(() => {
  const base = window.cinemaAdmin;
  window.cinemaAdmin = function () {
    const app = base();
    Object.assign(app, {
      libraryScanPlan: null, libraryScanPaths: [], libraryScanLoading: false, libraryScanError: '',
      async openLibraryScan() {
        this.modal = 'library-scan'; await this.refreshLibraryScan();
      },
      async refreshLibraryScan() {
        if (this.libraryScanLoading) return;
        this.libraryScanLoading = true; this.libraryScanError = '';
        try {
          const plan = await this.api('/operations?section=library_scan');
          if (this.modal !== 'library-scan') return;
          this.libraryScanPlan = plan;
          this.libraryScanPaths = plan.choices.filter(r => r.readable).map(r => r.path);
        } catch (e) { this.libraryScanError = e.message; }
        finally { this.libraryScanLoading = false; }
      },
      selectLibraryScan(row, checked) {
        let paths = this.libraryScanPaths.filter(p => p !== row.path);
        if (checked) {
          paths.push(row.path);
          if (row.kind === 'disk') paths.push(...this.libraryScanPlan.choices.filter(r => r.readable && r.path.startsWith(row.path + '/')).map(r => r.path));
        } else {
          paths = paths.filter(p => !this.libraryScanPlan.choices.some(r => r.kind === 'disk' && r.path === p && row.path.startsWith(p + '/')));
        }
        this.libraryScanPaths = [...new Set(paths)];
      },
      selectAllLibraryScan(checked) { this.libraryScanPaths = checked ? this.libraryScanPlan.choices.filter(r => r.readable).map(r => r.path) : []; },
      async runLibraryScan() {
        if (!this.libraryScanPaths.length || this.busy || this.libraryScanLoading) return;
        this.busy = true; this.libraryScanError = '';
        try {
          await this.api('/operations', { kind: 'media', action: 'library_scan', paths: this.libraryScanPaths });
          this.modal = null; await this.reload(false);
          this.notify('已按确认范围开始扫描，新内容保持未上架');
          await this.openOperations('tasks');
        } catch (e) { this.libraryScanError = e.message; }
        finally { this.busy = false; }
      }
    });
    return app;
  };
})();
