/* Guest suggestions share the console's session, permissions and save workflow. */
function guestRequestsUi() {
  return {
    guestDraft: null,
    guestDirty: false,
    guestRevision: 0,
    guestSettingsOpen: false,
    guestStates: {new: '待处理', processing: '处理中', done: '已处理'},
    guestLoaded(result) {
      if (this.guestDirty) return;
      if (!this.guestDraft) this.guestSettingsOpen = !result.settings.public_url;
      this.guestDraft = {
        ...result.settings,
        first_minutes: result.settings.first_delay_seconds / 60,
        interval_minutes: result.settings.interval_seconds / 60
      };
      this.guestRevision = this.data.revision;
    },
    async guestSettingsSave() {
      const draft = this.guestDraft;
      const result = await this.action('/operations', {
        kind: 'guest_request', action: 'guest_settings', _revision: this.guestRevision,
        settings: {
          enabled: !!draft.enabled, public_url: draft.public_url, prompt: draft.prompt,
          first_delay_seconds: Math.round(Number(draft.first_minutes) * 60),
          interval_seconds: Math.round(Number(draft.interval_minutes) * 60),
          visible_seconds: Number(draft.visible_seconds)
        }
      }, '扫码留言设置已保存', false);
      if (result) {
        this.guestDirty = false;
        this.guestSettingsOpen = false;
        await this.loadOperations();
      }
    },
    async guestStatusSave(row, status) {
      await this.action('/operations', {
        kind: 'guest_request', action: 'guest_status', id: row.id, status, version: row.version
      }, '留言状态已更新', false);
      await this.loadOperations();
    },
    guestTime(stamp) {
      return new Date(stamp).toLocaleString('zh-CN', {hour12: false});
    }
  };
}
