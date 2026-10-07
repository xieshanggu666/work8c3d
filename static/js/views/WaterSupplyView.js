/* 视图：枯水期供水保障 —— 水库管理员申报 / 调度员审核 / 乡镇确认优先级并执行配水 / 异常欠供追加应急物资 */
window.WaterSupplyView = {
  name: "WaterSupplyView",
  data() {
    return {
      plans: [],
      reservoirs: [],
      townships: [],
      supplies: [],
      disposals: [],
      logs: [],
      current: null,
      role: "manager",
      operatorNames: { manager: "孙库管", dispatcher: "张调度", township: "刘水务",
                       supply_manager: "陈物资" },
      actionNote: "",
      // 申报表单
      submitOpen: false,
      form: { reservoir_id: null, period_days: 7, title: "", reason: "",
              items: [{ township_id: null, planned_m3: null }] },
      // 优先级录入
      prioInputs: {},      // allocationId -> priority
      prioNotes: {},       // allocationId -> note
      // 实供上报
      deliveryInputs: {},  // allocationId -> delivered_m3
      // 应急追加弹窗
      emgOpen: false,
      emgForm: { supply_id: null, quantity: 1, township_id: null, disposal_id: null, note: "" },
      emgBusy: false,
    };
  },
  computed: {
    roles() {
      return [
        { id: "manager", name: "水库管理员" },
        { id: "dispatcher", name: "调度员" },
        { id: "township", name: "乡镇" },
        { id: "supply_manager", name: "物资管理员" },
      ];
    },
    roleText() {
      return { manager: "水库管理员", dispatcher: "调度员", township: "乡镇",
               supply_manager: "物资管理员" };
    },
    steps() {
      return [
        { key: "submitted", name: "申请", role: "水库管理员", actor: "submitted_by", at: "submitted_at" },
        { key: "reviewed", name: "审核", role: "调度员", actor: "reviewed_by", at: "reviewed_at" },
        { key: "executing", name: "执行配水", role: "乡镇", actor: "executed_by", at: "executed_at" },
        { key: "completed", name: "完成", role: "乡镇", actor: "completed_by", at: "completed_at" },
      ];
    },
    planReservoir() {
      return this.current
        ? this.reservoirs.find(r => r.id === this.current.reservoir_id) || null : null;
    },
    // 可挂接的防汛处置单（进行中/闭环均可，仅追加记录）
    linkableDisposals() { return this.disposals; },
    shortageRows() {
      return (this.current ? this.current.allocations : [])
        .filter(a => (a.shortage_m3 || 0) > 0);
    },
    totalShortage() {
      return this.current ? this.current.total_shortage_m3 : 0;
    },
  },
  methods: {
    statusBadge(st) {
      return { submitted: "orange", reviewed: "blue", executing: "yellow",
               completed: "green" }[st] || "gray";
    },
    statusText(st) {
      return { submitted: "待审核", reviewed: "待确认优先级", executing: "执行配水中",
               completed: "已完成" }[st] || st;
    },
    stepIndex(status) {
      return { submitted: 0, reviewed: 1, executing: 2, completed: 3 }[status] ?? -1;
    },
    droughtBadge(lv) {
      return { blue: "blue", yellow: "yellow", orange: "orange", red: "red" }[lv] || "gray";
    },
    droughtText(lv) {
      return { blue: "蓝色枯水预警", yellow: "黄色枯水预警",
               orange: "橙色枯水预警", red: "红色枯水预警" }[lv] || "";
    },
    async load() {
      try {
        const [plans, reservoirs, townships, supplies, disposals, logs] = await Promise.all([
          API.waterSupplyPlans(), API.reservoirsDrought(), API.townships(),
          API.supplies(), API.disposals(), API.dispatchLogs()]);
        this.plans = plans; this.reservoirs = reservoirs; this.townships = townships;
        this.supplies = supplies; this.disposals = disposals; this.logs = logs;
        if (this.current) {
          this.current = plans.find(p => p.id === this.current.id) || null;
          this.syncInputs();
        }
      } catch (e) {
        window.app.showToast("加载供水保障数据失败：" + e.message);
      }
    },
    selectPlan(p) { this.current = p; this.actionNote = ""; this.syncInputs(); },
    backToList() { this.current = null; },
    syncInputs() {
      this.prioInputs = {}; this.prioNotes = {}; this.deliveryInputs = {};
      if (this.current) {
        this.current.allocations.forEach((a, i) => {
          this.prioInputs[a.id] = a.priority || (i + 1);
          this.prioNotes[a.id] = a.priority_note || "";
          this.deliveryInputs[a.id] = a.delivered_m3 != null ? a.delivered_m3 : a.planned_m3;
        });
      }
    },
    remarks() {
      return this.current && this.current.remark ? this.current.remark.split("\n") : [];
    },

    // ---------------- 申报 ----------------
    openSubmit() {
      this.form = {
        reservoir_id: this.reservoirs.length ? this.reservoirs[0].id : null,
        period_days: 7, title: "", reason: "",
        items: [{ township_id: this.townships[0] ? this.townships[0].id : null,
                  planned_m3: null }],
      };
      this.submitOpen = true;
    },
    addItem() {
      const used = new Set(this.form.items.map(i => i.township_id));
      const next = this.townships.find(t => !used.has(t.id));
      this.form.items.push({ township_id: next ? next.id : null, planned_m3: null });
    },
    removeItem(idx) {
      if (this.form.items.length > 1) this.form.items.splice(idx, 1);
    },
    formTotal() {
      return this.form.items.reduce((s, i) => s + (Number(i.planned_m3) || 0), 0);
    },
    formReservoir() {
      return this.reservoirs.find(r => r.id === Number(this.form.reservoir_id));
    },
    async submitForm() {
      const f = this.form;
      if (!f.reservoir_id) { window.app.showToast("请选择供水水库"); return; }
      if (!(f.period_days > 0)) { window.app.showToast("供水周期须大于 0"); return; }
      const items = [];
      const seen = new Set();
      for (const it of f.items) {
        if (!it.township_id) { window.app.showToast("请为每条配水选择乡镇"); return; }
        if (seen.has(Number(it.township_id))) { window.app.showToast("同一乡镇重复，请合并"); return; }
        seen.add(Number(it.township_id));
        if (!(Number(it.planned_m3) > 0)) { window.app.showToast("配水量须大于 0"); return; }
        items.push({ township_id: Number(it.township_id), planned_m3: Number(it.planned_m3) });
      }
      try {
        const p = await API.submitWaterSupply({
          reservoir_id: Number(f.reservoir_id), period_days: Number(f.period_days),
          title: f.title, reason: f.reason, allocations: items,
          operator: this.operatorNames.manager, role: "manager",
        });
        window.app.showToast(`供水保障单 #${p.id} 已提交，待调度员审核`);
        this.submitOpen = false;
        await this.load();
        this.selectPlan(p);
      } catch (e) { window.app.showToast("提交失败：" + e.message); }
    },

    // ---------------- 审核 ----------------
    canReview() {
      return this.current && this.current.status === "submitted" && this.role === "dispatcher";
    },
    async review() {
      try {
        const p = await API.reviewWaterSupply(this.current.id, {
          operator: this.operatorNames.dispatcher, role: "dispatcher",
          opinion: this.actionNote.trim(),
        });
        window.app.showToast(`已审核：登记枯水/供水预警，转乡镇确认优先级`);
        this.actionNote = "";
        await this.load();
        this.selectPlan(p);
      } catch (e) { window.app.showToast("审核失败：" + e.message); }
    },

    // ---------------- 乡镇确认优先级 ----------------
    canSetPriority() {
      return this.current && this.current.status === "reviewed" && this.role === "township";
    },
    canConfirmPriorities() {
      // reviewed 状态下乡镇可启动；按钮内部再按确认情况禁用/放开
      return this.current && this.current.status === "reviewed" && this.role === "township";
    },
    async savePriority(a) {
      const pri = Number(this.prioInputs[a.id]);
      if (!(pri > 0)) { window.app.showToast("优先级须为大于 0 的整数（1 最高）"); return; }
      try {
        const p = await API.setWaterPriority(this.current.id, {
          township_id: a.township_id, priority: pri, note: this.prioNotes[a.id] || "",
          role: "township",
        });
        window.app.showToast(`「${a.township_name}」优先级已确认为第 ${pri} 顺位`);
        await this.load();
        this.selectPlan(p);
      } catch (e) { window.app.showToast("优先级确认失败：" + e.message); }
    },
    async confirmPriorities() {
      const missing = this.current.allocations.filter(a => !a.priority_confirmed);
      if (missing.length) {
        window.app.showToast(`还有 ${missing.length} 个乡镇未确认优先级`);
        return;
      }
      try {
        const p = await API.confirmWaterPriorities(this.current.id, {
          operator: this.operatorNames.township, role: "township",
          note: this.actionNote.trim(),
        });
        window.app.showToast("优先级全部确认，开始执行配水（预警进入处置中）");
        this.actionNote = "";
        await this.load();
        this.selectPlan(p);
      } catch (e) { window.app.showToast("启动配水失败：" + e.message); }
    },

    // ---------------- 执行上报 ----------------
    canReport() {
      return this.current && this.current.status === "executing" && this.role === "township";
    },
    async reportDelivery(a) {
      const v = Number(this.deliveryInputs[a.id]);
      if (!(v >= 0)) { window.app.showToast("实际供水量不能为负"); return; }
      try {
        const p = await API.reportWaterDelivery(this.current.id, {
          township_id: a.township_id, delivered_m3: v, role: "township",
        });
        window.app.showToast(`「${a.township_name}」已上报实供 ${v} m³`);
        await this.load();
        this.selectPlan(p);
      } catch (e) { window.app.showToast("上报失败：" + e.message); }
    },

    // ---------------- 完成 ----------------
    canComplete() {
      return this.current && this.current.status === "executing" && this.role === "township";
    },
    async complete() {
      try {
        const p = await API.completeWaterSupply(this.current.id, {
          operator: this.operatorNames.township, role: "township",
          summary: this.actionNote.trim(),
        });
        const c = p.completion || {};
        window.app.showToast(
          `已完成：放水 ${c.released_m3} m³、扣减库容，欠供 ${c.shortage_total_m3} m³`
          + (c.drought_level_after ? `（${this.droughtText(c.drought_level_after)}）` : "（预警销警）"));
        this.actionNote = "";
        await this.load();
        this.selectPlan(p);
      } catch (e) { window.app.showToast("完成失败：" + e.message); }
    },

    // ---------------- 异常欠供应急物资 ----------------
    canEmergency() {
      return this.current && ["executing", "completed"].includes(this.current.status)
        && this.role === "supply_manager" && this.totalShortage > 0;
    },
    openEmergency(a) {
      this.emgForm = {
        supply_id: this.supplies[0] ? this.supplies[0].id : null,
        quantity: 1,
        township_id: a ? a.township_id : null,
        disposal_id: null, note: "",
      };
      this.emgOpen = true;
    },
    emgTownName(tid) {
      const a = this.current.allocations.find(x => x.township_id === tid);
      return a ? a.township_name : "公用";
    },
    async submitEmergency() {
      const f = this.emgForm;
      if (!f.supply_id || !(f.quantity > 0)) {
        window.app.showToast("请选择物资并填写追加数量"); return;
      }
      this.emgBusy = true;
      try {
        const p = await API.addWaterEmergency(this.current.id, {
          supply_id: Number(f.supply_id), quantity: Number(f.quantity),
          township_id: f.township_id ? Number(f.township_id) : null,
          disposal_id: f.disposal_id ? Number(f.disposal_id) : null,
          note: f.note, operator: this.operatorNames.supply_manager,
          role: "supply_manager",
        });
        window.app.showToast("应急物资已直接出库追加");
        this.emgOpen = false;
        const [supplies] = await Promise.all([API.supplies(), this.load()]);
        this.supplies = supplies;
        this.selectPlan(p);
      } catch (e) { window.app.showToast("应急追加失败：" + e.message); }
      finally { this.emgBusy = false; }
    },
    pct(v, m) { return m > 0 ? Math.min(100, Math.round(v / m * 100)) : 0; },
    logKindText(k) { return k === "water_supply" ? "供水调度" : "防洪调度"; },
  },
  mounted() { this.load(); },
  template: `
  <div class="page">
    <div class="page-title">枯水期供水保障
      <span class="sub">水库管理员申报 · 调度员审核 · 乡镇确认优先级并执行配水 · 完成扣减库容回写调度预警</span>
      <button class="btn sm" style="margin-left:auto" @click="load">刷新</button>
    </div>

    <!-- 角色切换 -->
    <div class="panel">
      <div class="panel-body" style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
        <span style="font-size:12.5px;color:#7d95b4">当前值守角色</span>
        <button v-for="r in roles" :key="r.id" class="btn sm"
                :class="{primary: role===r.id}" @click="role=r.id">{{ r.name }}</button>
        <div class="field" style="margin-left:auto">
          <label>操作人（电子签名）</label>
          <input v-model="operatorNames[role]" class="role-input" style="min-width:150px"/>
        </div>
      </div>
    </div>

    <!-- ============ 列表视图 ============ -->
    <template v-if="!current">
      <!-- 水库枯水态势 -->
      <div class="stats">
        <div class="stat blue"><div class="k">供水水库</div><div class="v">{{ reservoirs.length }}<small>座</small></div></div>
        <div class="stat red"><div class="k">枯水预警水库</div><div class="v">{{ reservoirs.filter(r=>r.drought_level).length }}<small>座</small></div></div>
        <div class="stat amber"><div class="k">进行中保障单</div><div class="v">{{ plans.filter(p=>p.status!=='completed').length }}<small>张</small></div></div>
        <div class="stat green"><div class="k">已完成保障</div><div class="v">{{ plans.filter(p=>p.status==='completed').length }}<small>张</small></div></div>
      </div>

      <div class="row">
        <div class="col col-2">
          <div class="panel">
            <div class="panel-head">水库枯水与可供水态势
              <button v-if="role==='manager'" class="btn sm primary" style="margin-left:auto" @click="openSubmit">＋ 提交供水计划</button>
            </div>
            <div class="panel-body nopad">
              <table class="grid">
                <thead><tr><th>水库</th><th>当前水位</th><th>死水位</th><th>枯水预警线</th><th>当前蓄水</th><th>死水位以上可供水</th><th>枯水等级</th></tr></thead>
                <tbody>
                  <tr v-for="r in reservoirs" :key="r.id">
                    <td>{{ r.name }}</td>
                    <td class="num mono">{{ fmt.num(r.current_level,2) }} m</td>
                    <td class="num mono">{{ fmt.num(r.dead_level,2) }}</td>
                    <td class="num mono">{{ fmt.num(r.drought_warn_level,2) }}</td>
                    <td class="num mono">{{ fmt.num(r.current_storage,1) }} 万m³</td>
                    <td class="num mono">{{ fmt.num(r.available_water_m3,0) }} m³</td>
                    <td>
                      <span v-if="r.drought_level" class="badge" :class="droughtBadge(r.drought_level)">
                        {{ droughtText(r.drought_level) }}
                      </span>
                      <span v-else class="badge green">正常</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
        <div class="col col-1">
          <div class="panel">
            <div class="panel-head">受水乡镇 <span class="tag">{{ townships.length }} 个</span></div>
            <div class="panel-body nopad" style="max-height:260px;overflow-y:auto">
              <table class="grid">
                <thead><tr><th>乡镇</th><th>联系人</th><th>日均需水</th></tr></thead>
                <tbody>
                  <tr v-for="t in townships" :key="t.id">
                    <td>{{ t.name }}</td><td style="font-size:12px">{{ t.contact }}</td>
                    <td class="num">{{ t.demand_m3 }} m³/日</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <div class="panel" v-if="plans.length">
        <div class="panel-head">供水保障单台账 <span class="tag">{{ plans.length }} 张</span></div>
        <div class="panel-body nopad">
          <table class="grid">
            <thead><tr><th>单号</th><th>标题</th><th>水库</th><th>周期</th><th>计划/实供/欠供 (m³)</th><th>状态</th><th>应急追加</th><th>申报时间</th><th></th></tr></thead>
            <tbody>
              <tr v-for="p in plans" :key="p.id" style="cursor:pointer" @click="selectPlan(p)">
                <td class="mono">#{{ p.id }}</td>
                <td>{{ p.title }}</td>
                <td>{{ p.reservoir_name }}</td>
                <td class="num">{{ p.period_days }} 日</td>
                <td class="num mono">
                  {{ fmt.num(p.total_planned_m3,0) }} /
                  <span :style="p.total_shortage_m3>0 ? 'color:var(--orange-lv)' : ''">{{ fmt.num(p.total_delivered_m3,0) }}</span> /
                  <span :style="p.total_shortage_m3>0 ? 'color:#ff6b7a;font-weight:700' : ''">{{ fmt.num(p.total_shortage_m3,0) }}</span>
                </td>
                <td><span class="badge" :class="statusBadge(p.status)">{{ p.status_text }}</span></td>
                <td>{{ p.emergencies.length ? p.emergencies.length + ' 次' : '—' }}</td>
                <td style="font-size:11.5px;color:#7d95b4">{{ fmt.time(p.submitted_at) }}</td>
                <td style="text-align:right"><button class="btn sm" @click.stop="selectPlan(p)">详情</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 水库调度记录 -->
      <div class="panel" v-if="logs.length">
        <div class="panel-head">水库调度记录 <span class="tag">供水放水回写 · 与防洪调度并存</span></div>
        <div class="panel-body nopad" style="max-height:300px;overflow-y:auto">
          <table class="grid">
            <thead><tr><th>时间</th><th>水库</th><th>类型</th><th>事项</th><th>放水量(m³)</th><th>放水前→后水位</th><th>放水后库容</th><th>操作人</th></tr></thead>
            <tbody>
              <tr v-for="l in logs" :key="l.id">
                <td style="font-size:11.5px;color:#7d95b4">{{ fmt.time(l.created_at) }}</td>
                <td>{{ l.reservoir_name }}</td>
                <td><span class="badge" :class="l.kind==='water_supply' ? 'blue' : 'cyan'">{{ logKindText(l.kind) }}</span></td>
                <td style="font-size:12px">{{ l.title }}</td>
                <td class="num mono">{{ fmt.num(l.released_m3,0) }}</td>
                <td class="num mono">{{ fmt.num(l.level_before,2) }} → {{ fmt.num(l.level_after,2) }}</td>
                <td class="num mono">{{ fmt.num(l.storage_after,1) }} 万m³</td>
                <td style="font-size:11.5px;color:#7d95b4">{{ l.created_by }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <!-- ============ 详情视图 ============ -->
    <template v-else>
      <div class="panel">
        <div class="panel-body">
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
            <button class="btn sm" @click="backToList">← 返回</button>
            <div style="font-size:16px;font-weight:700">供水保障单 #{{ current.id }} · {{ current.title }}</div>
            <span class="badge" :class="statusBadge(current.status)">{{ current.status_text }}</span>
            <span v-if="current.total_shortage_m3 > 0" class="badge orange">欠供 {{ fmt.num(current.total_shortage_m3,0) }} m³</span>
            <span style="margin-left:auto;font-size:12px;color:#7d95b4">
              {{ current.reservoir_name }} · 计划周期 {{ current.period_days }} 日
            </span>
          </div>
          <div v-if="current.reason" style="margin-top:8px;font-size:12.5px;color:#b9cbe2">事由：{{ current.reason }}</div>
        </div>
      </div>

      <!-- 四态流转 -->
      <div class="panel">
        <div class="panel-head">协同流转 <span class="tag">申请 → 审核 → 乡镇确认优先级并执行配水 → 完成扣减库容</span></div>
        <div class="panel-body">
          <div class="flow-steps">
            <template v-for="(s, i) in steps" :key="s.key">
              <div class="flow-step" :class="{done: stepIndex(current.status) >= i, active: current.status===s.key}">
                <div class="dot">{{ i + 1 }}</div>
                <div class="meta">
                  <div class="name">{{ s.name }}</div>
                  <div class="role">{{ s.role }}</div>
                  <div class="who" v-if="current[s.actor]">{{ current[s.actor] }} · {{ fmt.time(current[s.at]) }}</div>
                </div>
              </div>
              <div class="flow-arrow" v-if="i < steps.length - 1">→</div>
            </template>
          </div>

          <div style="margin-top:16px;display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
            <div class="field" style="flex:1;min-width:240px" v-if="canReview()||canConfirmPriorities()||canComplete()">
              <label>{{ canReview() ? '审核意见' : canComplete() ? '完成小结' : '优先级确认说明' }}（可选）</label>
              <input v-model="actionNote" :placeholder="canReview() ? '如：同意按计划供水，加强旱情监测' : '记录说明…'"/>
            </div>
            <button v-if="canReview()" class="btn primary" @click="review">✓ 审核通过并登记供水预警</button>
            <button v-if="canConfirmPriorities()" class="btn primary"
                    :disabled="!current.all_priority_confirmed" @click="confirmPriorities">
              ▶ 全部优先级已确认，启动配水
            </button>
            <button v-if="canComplete()" class="btn primary" @click="complete">
              ✔ 完成配水并扣减库容、回写调度预警
            </button>
            <span v-if="current.status==='completed'" class="badge green">保障已闭环</span>
            <span v-else-if="!canReview() && !canConfirmPriorities() && !canComplete()"
                  style="font-size:12.5px;color:#7d95b4">
              当前角色（{{ roleText[role] }}）本环节无待办，可切换角色继续流转
            </span>
          </div>
        </div>
      </div>

      <!-- 水量总览 -->
      <div class="stats">
        <div class="stat blue"><div class="k">计划总供水</div><div class="v">{{ fmt.num(current.total_planned_m3,0) }}<small>m³</small></div></div>
        <div class="stat green"><div class="k">实际已供水</div><div class="v">{{ fmt.num(current.total_delivered_m3,0) }}<small>m³</small></div></div>
        <div class="stat" :class="current.total_shortage_m3>0 ? 'red' : ''">
          <div class="k">欠供水量</div><div class="v">{{ fmt.num(current.total_shortage_m3,0) }}<small>m³</small></div>
        </div>
        <div class="stat amber"><div class="k">应急物资追加</div><div class="v">{{ current.emergencies.length }}<small>次</small></div></div>
        <div class="stat" v-if="planReservoir">
          <div class="k">死水位以上尚可放</div>
          <div class="v">{{ fmt.num(planReservoir.available_water_m3,0) }}<small>m³</small></div>
        </div>
      </div>

      <!-- 配水明细 -->
      <div class="panel">
        <div class="panel-head">分乡镇配水与优先级 <span class="tag">乡镇审核通过后逐户确认优先级（1 最高）</span></div>
        <div class="panel-body nopad">
          <table class="grid">
            <thead>
              <tr><th>受水乡镇</th><th>计划配水(m³)</th><th>优先级顺位</th><th>优先级确认</th>
                  <th>实际供水(m³)</th><th>欠供(m³)</th>
                  <th style="min-width:260px">岗位操作</th></tr>
            </thead>
            <tbody>
              <tr v-for="a in current.allocations" :key="a.id">
                <td>{{ a.township_name }}</td>
                <td class="num mono">{{ fmt.num(a.planned_m3,0) }}</td>
                <td>
                  <span v-if="a.priority_confirmed" class="badge cyan">第 {{ a.priority }} 顺位</span>
                  <span v-else class="badge gray">未确认</span>
                </td>
                <td style="font-size:11.5px;color:#7d95b4">
                  <div v-if="a.priority_note">{{ a.priority_note }}</div>
                  <div>{{ a.priority_confirmed ? '乡镇已确认' : '待乡镇确认' }}</div>
                </td>
                <td class="num mono">{{ a.delivered_m3 != null ? fmt.num(a.delivered_m3,0) : '—' }}</td>
                <td class="num mono">
                  <span :style="(a.shortage_m3||0)>0 ? 'color:#ff6b7a;font-weight:700' : ''">
                    {{ a.shortage_m3 != null ? fmt.num(a.shortage_m3,0) : '—' }}
                  </span>
                </td>
                <td>
                  <!-- 乡镇确认优先级 -->
                  <div v-if="canSetPriority() && !a.priority_confirmed" style="display:flex;gap:6px;align-items:center">
                    <input type="number" min="1" v-model.number="prioInputs[a.id]" style="width:70px" title="优先级（1 最高）"/>
                    <input v-model="prioNotes[a.id]" placeholder="确认说明（可选）" style="flex:1;min-width:120px"/>
                    <button class="btn xs primary" @click="savePriority(a)">确认优先级</button>
                  </div>
                  <div v-else-if="canSetPriority() && a.priority_confirmed" style="font-size:11.5px;color:#7dd3a0">
                    ✓ 已确认第 {{ a.priority }} 顺位
                  </div>
                  <!-- 乡镇执行上报 -->
                  <div v-if="canReport()" style="display:flex;gap:6px;align-items:center;margin-top:4px">
                    <input type="number" min="0" :max="a.planned_m3" v-model.number="deliveryInputs[a.id]" style="width:110px"/>
                    <button class="btn xs primary" @click="reportDelivery(a)">上报实供</button>
                  </div>
                  <!-- 欠供应急 -->
                  <button v-if="(a.shortage_m3||0)>0 && role==='supply_manager' && ['executing','completed'].includes(current.status)"
                          class="btn xs" style="margin-top:4px" @click="openEmergency(a)">＋ 追加应急物资</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 应急物资追加 -->
      <div class="row">
        <div class="col col-2">
          <div class="panel">
            <div class="panel-head">异常欠供 · 应急物资追加 <span class="tag">物资管理员 · 直接出库 · 复用应急物资库存</span></div>
            <div class="panel-body nopad">
              <div style="padding:10px 14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
                <button v-if="canEmergency()" class="btn sm primary" @click="openEmergency(null)">＋ 追加应急物资（整单公用）</button>
                <span v-else-if="current.status==='completed'" style="font-size:12px;color:#7d95b4">
                  {{ current.total_shortage_m3 > 0 ? '切换「物资管理员」角色可继续补拨应急物资' : '本次供水无欠供，无需应急物资' }}
                </span>
                <span v-else style="font-size:12px;color:#7d95b4">执行配水出现欠供后，由物资管理员在此追加应急物资</span>
              </div>
              <table class="grid">
                <thead><tr><th>时间</th><th>物资</th><th>数量</th><th>投向乡镇</th><th>认定欠供(m³)</th><th>挂接处置单</th><th>说明</th><th>操作人</th></tr></thead>
                <tbody>
                  <tr v-for="e in current.emergencies" :key="e.id">
                    <td style="font-size:11.5px;color:#7d95b4">{{ fmt.time(e.created_at) }}</td>
                    <td>{{ e.supply_name }}</td>
                    <td class="num">{{ e.quantity }} {{ e.unit }}</td>
                    <td>{{ e.township_id ? emgTownName(e.township_id) : '整单公用' }}</td>
                    <td class="num mono">{{ fmt.num(e.shortage_m3,0) }}</td>
                    <td>
                      <span v-if="e.disposal_id" class="badge cyan" style="cursor:pointer"
                            @click="$root.view='disposal'">#{{ e.disposal_id }}</span>
                      <span v-else class="badge gray">未挂接</span>
                    </td>
                    <td style="font-size:11.5px;color:#b9cbe2">{{ e.note || '—' }}</td>
                    <td style="font-size:11.5px;color:#7d95b4">{{ e.created_by }}</td>
                  </tr>
                  <tr v-if="!current.emergencies.length">
                    <td colspan="8" style="text-align:center;color:#7d95b4;padding:18px">暂无应急物资追加记录</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <!-- 回写情况 -->
        <div class="col col-1">
          <div class="panel">
            <div class="panel-head">调度与预警回写</div>
            <div class="panel-body" style="display:flex;flex-direction:column;gap:12px;font-size:13px">
              <div class="writeback-item">
                <span class="badge" :class="stepIndex(current.status)>=1 ? 'green' : 'gray'">{{ stepIndex(current.status)>=1 ? '已登记' : '待审核' }}</span>
                <span>枯水/供水预警挂接保障单（{{ current.warning_count }} 条）</span>
              </div>
              <div class="writeback-item">
                <span class="badge" :class="stepIndex(current.status)>=2 ? 'green' : 'gray'">{{ stepIndex(current.status)>=2 ? '处置中' : '待执行' }}</span>
                <span>启动配水后供水预警进入处置中</span>
              </div>
              <div class="writeback-item">
                <span class="badge" :class="current.status==='completed' ? 'green' : 'gray'">{{ current.status==='completed' ? '已回写' : '待完成' }}</span>
                <span>按实供水量扣减水库库容/水位，追加调度记录</span>
              </div>
              <div class="writeback-item" v-if="current.completion">
                <span class="badge" :class="current.completion.drought_level_after ? 'red' : 'green'">
                  {{ current.completion.drought_level_after ? droughtText(current.completion.drought_level_after) : '预警销警' }}
                </span>
                <span>放水后水位 {{ fmt.num(current.completion.level_after,2) }} m
                  （死水位 {{ fmt.num(current.completion.dead_level,2) }} m）</span>
              </div>
            </div>
          </div>
          <div class="panel" v-if="remarks().length">
            <div class="panel-head">处置记录</div>
            <div class="panel-body" style="font-size:12.5px;color:#b9cbe2;line-height:1.9">
              <div v-for="(line, i) in remarks()" :key="i">{{ line }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- 申报快照 / 完成快照 -->
      <div class="row" v-if="current.plan || current.completion">
        <div class="col col-1" v-if="current.plan">
          <div class="panel">
            <div class="panel-head">申报时水库工况 <span class="tag">计划快照</span></div>
            <div class="panel-body" style="font-size:13px;line-height:2">
              <div>申报水位：<b>{{ fmt.num(current.plan.reservoir.level,2) }}</b> m
                ｜蓄水：<b>{{ fmt.num(current.plan.reservoir.storage,1) }}</b> 万m³</div>
              <div>死水位：{{ fmt.num(current.plan.reservoir.dead_level,2) }} m
                ｜枯水预警线：{{ fmt.num(current.plan.reservoir.drought_warn_level,2) }} m</div>
              <div>申报可供水：{{ fmt.num(current.plan.available_water_m3,0) }} m³
                ｜日均计划：{{ fmt.num(current.plan.daily_planned_m3,0) }} m³/日</div>
            </div>
          </div>
        </div>
        <div class="col col-1" v-if="current.completion">
          <div class="panel">
            <div class="panel-head">完成结算 <span class="tag">扣减库容回写</span></div>
            <div class="panel-body" style="font-size:13px;line-height:2">
              <div>实际放水：<b style="color:#7dd3a0">{{ fmt.num(current.completion.released_m3,0) }}</b> m³
                ｜欠供：<b :style="current.completion.shortage_total_m3>0 ? 'color:#ff8d97' : ''">{{ fmt.num(current.completion.shortage_total_m3,0) }}</b> m³</div>
              <div>库容：{{ fmt.num(current.completion.storage_before,1) }} →
                <b>{{ fmt.num(current.completion.storage_after,1) }}</b> 万m³</div>
              <div>水位：{{ fmt.num(current.completion.level_before,2) }} →
                <b>{{ fmt.num(current.completion.level_after,2) }}</b> m</div>
              <div>应急物资追加：{{ current.completion.emergency_count }} 次
                ｜调度记录 #{{ current.completion.dispatch_log_id }}</div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 申报弹窗 -->
    <div v-if="submitOpen" class="modal-mask" @click.self="submitOpen=false">
      <div class="modal">
        <div class="modal-head">
          提交枯水期供水保障计划
          <button class="modal-close" @click="submitOpen=false">×</button>
        </div>
        <div class="modal-body">
          <div class="field">
            <label>供水水库</label>
            <select v-model.number="form.reservoir_id">
              <option v-for="r in reservoirs" :key="r.id" :value="r.id">
                {{ r.name }}（水位 {{ fmt.num(r.current_level,2) }}m，死水位以上可供水 {{ fmt.num(r.available_water_m3,0) }} m³）
              </option>
            </select>
          </div>
          <div style="display:flex;gap:10px">
            <div class="field" style="flex:1">
              <label>计划供水周期（日）</label>
              <input type="number" min="1" v-model.number="form.period_days"/>
            </div>
            <div class="field" style="flex:2">
              <label>计划标题（可选）</label>
              <input v-model="form.title" placeholder="默认按水库名称生成"/>
            </div>
          </div>
          <div class="field">
            <label>枯水事由（可选）</label>
            <input v-model="form.reason" placeholder="如：连续 30 日无有效降雨，下游乡镇饮水告急"/>
          </div>
          <div class="field">
            <label>分乡镇配水计划（m³）</label>
            <table class="grid">
              <thead><tr><th>受水乡镇</th><th>计划配水量</th><th></th></tr></thead>
              <tbody>
                <tr v-for="(it, idx) in form.items" :key="idx">
                  <td>
                    <select v-model.number="it.township_id">
                      <option v-for="t in townships" :key="t.id" :value="t.id">
                        {{ t.name }}（日均需水 {{ t.demand_m3 }} m³）
                      </option>
                    </select>
                  </td>
                  <td><input type="number" min="1" v-model.number="it.planned_m3" style="width:130px"/></td>
                  <td style="text-align:right">
                    <button class="btn xs danger" @click="removeItem(idx)" :disabled="form.items.length<=1">删除</button>
                  </td>
                </tr>
              </tbody>
            </table>
            <button class="btn sm" style="margin-top:8px" @click="addItem">＋ 增加乡镇</button>
            <div style="margin-top:10px;font-size:13px">
              合计申报：<b :style="formReservoir() && formTotal()>formReservoir().available_water_m3 ? 'color:#ff6b7a' : 'color:#7dd3a0'">
                {{ fmt.num(formTotal(),0) }} m³</b>
              <span v-if="formReservoir()" style="color:#7d95b4">
                ｜死水位以上可供水 {{ fmt.num(formReservoir().available_water_m3,0) }} m³
              </span>
            </div>
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="submitOpen=false">取消</button>
          <button class="btn primary" @click="submitForm">提交审核</button>
        </div>
      </div>
    </div>

    <!-- 应急物资追加弹窗 -->
    <div v-if="emgOpen" class="modal-mask" @click.self="emgOpen=false">
      <div class="modal">
        <div class="modal-head">
          异常欠供 · 追加应急物资
          <button class="modal-close" @click="emgOpen=false">×</button>
        </div>
        <div class="modal-body">
          <p style="font-size:12.5px;color:#b9cbe2;line-height:1.9;margin:0 0 12px">
            当前认定欠供 <b style="color:#ff8d97">{{ fmt.num(totalShortage,0) }} m³</b>。
            物资管理员从应急物资库存直接出库追加（与防汛处置共用库存池）；
            可挂接既有防汛处置单，仅在其处置记录追加一行，<b style="color:#7dd3a0">不改变其状态与资源台账</b>。
          </p>
          <div class="field">
            <label>应急物资</label>
            <select v-model.number="emgForm.supply_id">
              <option v-for="s in supplies" :key="s.id" :value="s.id">
                {{ s.name }} · 库存 {{ s.stock }}{{ s.unit }}{{ s.low ? '（低于安全库存）' : '' }}
              </option>
            </select>
          </div>
          <div style="display:flex;gap:10px">
            <div class="field" style="flex:1">
              <label>追加数量</label>
              <input type="number" min="1" v-model.number="emgForm.quantity"/>
            </div>
            <div class="field" style="flex:1">
              <label>投向乡镇</label>
              <select v-model.number="emgForm.township_id">
                <option :value="null">整单公用</option>
                <option v-for="a in shortageRows" :key="a.id" :value="a.township_id">
                  {{ a.township_name }}（欠供 {{ fmt.num(a.shortage_m3,0) }} m³）
                </option>
              </select>
            </div>
          </div>
          <div class="field">
            <label>挂接既有防汛处置单（可选，兼容处置记录）</label>
            <select v-model.number="emgForm.disposal_id">
              <option :value="null">不挂接</option>
              <option v-for="d in linkableDisposals" :key="d.id" :value="d.id">
                #{{ d.id }} {{ d.title }}（{{ d.status_text }}）
              </option>
            </select>
          </div>
          <div class="field">
            <label>说明（可选）</label>
            <input v-model="emgForm.note" placeholder="如：安排送水车向缺水村点配送瓶装水"/>
          </div>
        </div>
        <div class="modal-foot">
          <button class="btn" @click="emgOpen=false">取消</button>
          <button class="btn primary" :disabled="emgBusy" @click="submitEmergency">
            {{ emgBusy ? "出库中…" : "确认追加并直接出库" }}
          </button>
        </div>
      </div>
    </div>
  </div>`,
};
