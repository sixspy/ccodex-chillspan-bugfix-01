export const reconcileViewTemplate = `
<section class="panel">
  <h2>仓间对账互证</h2>
  <div class="form-row">
    <div><label>仓间编码</label><input v-model="reconCode"/></div>
    <div><label>批次互证编码</label><input v-model="proofCode"/></div>
  </div>
  <div class="actions">
    <button @click="loadRecon">对账单仓</button>
    <button class="secondary" @click="loadProof">批次互证</button>
    <button class="secondary" @click="loadReconAll">全仓对账</button>
  </div>
  <div v-if="reconOne" class="detail-box">
    <p>配额 {{ reconOne.quota_batches }} · 在批 {{ reconOne.active_batches }} · <span :class="reconOne.balanced ? 'ok' : 'bad'">{{ reconOne.balanced ? "平衡" : "失衡" }}</span></p>
  </div>
  <div v-if="proofOne" class="detail-box">
    <p>状态 {{ proofOne.status }} · 开放游标 {{ proofOne.open_excursions }} · 温谱 {{ proofOne.spectrum_exports }}</p>
    <p>血统：
      <span class="badge" :class="proofOne.lineage_complete ? 'pass' : 'fail'">
        {{ proofOne.lineage_complete ? "血统完整" : "血统断链" }}
      </span>
      <template v-if="proofOne.lineage">
        （{{ proofOne.lineage.lineage_required ? ("父批 " + proofOne.lineage.parent_code + " · 正式入边 " + proofOne.lineage.formal_inbound_edges) : "独立批无血统义务" }}）
      </template>
    </p>
  </div>
  <table class="data-table">
    <thead><tr><th>仓间</th><th>区域</th><th>在批</th><th>余配额</th><th>平衡</th><th>断链批</th></tr></thead>
    <tbody>
      <tr v-for="r in reconAllItems" :key="r.chamber_code">
        <td>{{ r.chamber_code }}</td>
        <td>{{ r.region }}</td>
        <td>{{ r.active_batches }}</td>
        <td>{{ r.quota_left }}</td>
        <td>{{ r.balanced ? "是" : "否" }}</td>
        <td><span class="badge" :class="r.lineage_broken_count ? 'fail' : 'pass'">{{ r.lineage_broken_count }}</span></td>
      </tr>
    </tbody>
  </table>
</section>
`;