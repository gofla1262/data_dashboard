import io
import sqlite3

import pandas as pd
import plotly.express as px
import streamlit as st

ACCENT, GRAY = "#1F3A5F", "#BDBDBD"          # ← 여기부터 추가
px.defaults.color_discrete_map = {
    "Android": ACCENT, "iOS": GRAY,
    "push": ACCENT, "organic": "#8C8C8C", "paid_ad": GRAY, "referral": "#DADADA",
}
px.defaults.color_discrete_sequence = [GRAY]

st.set_page_config(page_title="사용자 행동 탐색 대시보드", layout="wide")
st.title("사용자 행동 탐색 대시보드")
st.caption("주별 세션 퍼널 | 플랫폼 · 유입 경로별 | 2025.07 ~ 2026.06")

# ------------------------------------------------------------
# 업로드된 파일 확인 및 관리 (캐시 함수 밖에서 수행)
# ------------------------------------------------------------
uploaded_file = st.session_state.get("upload_file", None)
file_bytes = None
data_source_name = "data 폴더 기본 파일"
NEEDED_COLS_CHECK = ["event_id", "event_datetime", "customer_id", "session_id", "event_name", "platform", "traffic_source"]

if uploaded_file is not None:
    try:
        # 업로드된 파일의 컬럼 검증을 위해 pandas로 먼저 읽어봄
        preview_df = pd.read_csv(uploaded_file, nrows=0)
        missing_cols = [col for col in NEEDED_COLS_CHECK if col not in preview_df.columns]
        
        if missing_cols:
            st.error(f"업로드된 파일에 필요한 컬럼이 없습니다. 빠진 컬럼: {', '.join(missing_cols)}. 기본 파일을 사용합니다.")
            uploaded_file = None
            file_bytes = None
        else:
            uploaded_file.seek(0)
            file_bytes = uploaded_file.getvalue()
            data_source_name = f"올린 파일 {uploaded_file.name}"
    except Exception as e:
        st.error(f"파일을 읽는 중 오류가 발생했습니다: {e}. 기본 파일을 사용합니다.")
        uploaded_file = None
        file_bytes = None

# 업로드 파일 변경 감지 및 필터 초기화 번호 관리
if "last_uploaded_file_name" not in st.session_state:
    st.session_state.last_uploaded_file_name = uploaded_file.name if uploaded_file else None

current_file_name = uploaded_file.name if uploaded_file else None
if current_file_name != st.session_state.last_uploaded_file_name:
    if "reset_count" not in st.session_state:
        st.session_state.reset_count = 0
    st.session_state.reset_count += 1
    st.session_state.last_uploaded_file_name = current_file_name

# ------------------------------------------------------------
# 0. 필요한 데이터만 추출 — 쓸 테이블과 컬럼만 메모리 DB에 올리고, 정제 테이블 events를 처음 한 번만 만듦
# ------------------------------------------------------------
DATA_DIR = "data"                         # 원본 CSV를 넣어 둔 폴더
NEEDED = {                                # 이번 대시보드에 필요한 테이블과 컬럼
    "app_events": NEEDED_COLS_CHECK,
}

@st.cache_resource
def get_db(file_bytes_arg):               # 필요한 컬럼만 메모리 DB에 올리고, 정제 테이블 events를 만듦 (처음 한 번만)
    con = sqlite3.connect(":memory:", check_same_thread=False)
    if file_bytes_arg is not None:
        df_uploaded = pd.read_csv(io.BytesIO(file_bytes_arg), usecols=NEEDED["app_events"])
        df_uploaded.to_sql("app_events", con, index=False)
    else:
        for name, cols in NEEDED.items():
            pd.read_csv(f"{DATA_DIR}/{name}.csv", usecols=cols).to_sql(name, con, index=False)
            
    # 5주차 제외 규칙: QA 계정 제외 · 중복 이벤트 제거
    con.execute("""
        CREATE TABLE events AS
        SELECT event_id, event_datetime, customer_id, session_id, event_name, platform, traffic_source
          FROM (SELECT *,
                       ROW_NUMBER() OVER (PARTITION BY session_id, event_datetime, event_name
                                          ORDER BY event_id) AS dup
                  FROM app_events
                 WHERE customer_id NOT LIKE 'QA%')
         WHERE dup = 1
    """)
    # 세션별로 묶는 집계가 빨라지도록 session_id에 인덱스를 붙임 (없으면 첫 실행이 몇 분 걸릴 수 있음)
    con.execute("CREATE INDEX idx_events_session ON events(session_id)")
    return con

# ------------------------------------------------------------
# 1. 데이터 준비 — 정제된 events 테이블을 SQL로 집계
# ------------------------------------------------------------
def query(sql, file_bytes_arg):           # SQL을 실행해 표로 돌려줌
    return pd.read_sql(sql, get_db(file_bytes_arg))

# 주 × 플랫폼 × 유입 경로별 세션 퍼널
SQL_WEEKLY_FUNNEL = """
WITH s AS (                                 -- 세션의 첫 이벤트로 플랫폼 · 유입 경로 · 주 정하기
    SELECT session_id, platform, traffic_source,
           DATE(MIN(event_datetime), 'weekday 0', '-6 days') AS 주시작일
      FROM events
     GROUP BY session_id
)
SELECT s.주시작일,
       s.platform     AS 플랫폼,
       s.traffic_source AS 유입경로,
       COUNT(DISTINCT e.session_id)                                                                                                   AS 세션수,
       COUNT(DISTINCT CASE WHEN e.event_name = 'view_restaurant' THEN e.session_id END)   AS 조회세션,
       COUNT(DISTINCT CASE WHEN e.event_name = 'add_to_cart'    THEN e.session_id END)   AS 장바구니세션,
       COUNT(DISTINCT CASE WHEN e.event_name = 'begin_checkout'  THEN e.session_id END)   AS 결제시작세션,
       COUNT(DISTINCT CASE WHEN e.event_name = 'order_complete'  THEN e.session_id END)   AS 주문세션,
       SUM(CASE WHEN e.event_name = 'payment_fail' THEN 1 ELSE 0 END)                     AS 결제실패
  FROM events e
  JOIN s ON e.session_id = s.session_id
 WHERE s.주시작일 < '2026-06-29'           -- 이틀치뿐인 마지막 주 제외
 GROUP BY 1, 2, 3
 ORDER BY 1, 2, 3
"""

@st.cache_data
def load_data(file_bytes_arg):
    data = query(SQL_WEEKLY_FUNNEL, file_bytes_arg)
    data["주시작일"] = pd.to_datetime(data["주시작일"]).dt.date
    return data

df = load_data(file_bytes)                # 주 × 플랫폼 × 유입 경로별 세션 퍼널

AI_NOTES = {
"전체": "전체 세션 수는 28,840개이며 주문 전환율은 14.5%, 가장 낮은 단계 전환율은 장바구니 단계의 41.4%이다. 전체 기간 동안 플랫폼과 경로별로 전환 성과에 차이가 나타난 것으로 보인다.",
"Android": "Android는 세션 수 16,053개, 주문 전환율 14.3%, 장바구니 단계 전환율 41.8%이며, 2026-04-20 주부터 2026-05-25 주까지 결제 전환율이 크게 하락했다. 해당 기간 동안 결제 전환율 급락의 원인 확인이 필요하다.",
"iOS": "iOS는 세션 수 12,787개, 주문 전환율 14.8%, 장바구니 단계 전환율 40.8%로 Android와 유사하나 특정 시기의 장기 급락 없이 안정적이었다. 전반적으로 비교적 일정한 퍼널 흐름을 유지한 것으로 보인다.",
"organic": "organic은 세션 수 22,489개, 주문 전환율 16.0%, 장바구니 단계 전환율 41.4%로 전체 평균과 유사한 수치를 보였다. 전체 유입의 대다수를 차지하며 자연 유입을 통해 안정적인 성과를 낸 것으로 보인다.",
"paid_ad": "paid_ad는 세션 수 1,595개, 주문 전환율 26.3%, 장바구니 단계 전환율 53.7%로 타 경로 대비 전환율이 월등히 높다. 구매 의도가 높은 사용자가 효과적으로 유입된 것으로 보인다.",
"push": "push는 세션 수 4,635개, 주문 전환율 3.2%, 가장 낮은 단계 전환율은 조회 단계의 28.4%이다. 첫 단계인 조회 단계의 이탈이 커서 알림 메시지 연결 경험에 대한 점검이 필요하다.",
"referral": "referral은 세션 수 121개로 500개 미만이며 주문 전환율은 0.0%, 가장 낮은 단계 전환율은 주문 단계의 0.0%이다. 모수가 매우 적어 성과를 단정하기 어렵고 유입 링크의 정상 작동 여부 확인이 필요하다."
}

AI_SOURCE = "bot: Gemini가 미리 만든 해설 · 전체 기간 기준"

# ------------------------------------------------------------
# 사이드바 필터 및 초기화 버튼 관리
# ------------------------------------------------------------
if "reset_count" not in st.session_state:
    st.session_state.reset_count = 0

rc = st.session_state.reset_count

st.sidebar.header("필터")

if st.sidebar.button("필터 초기화"):
    st.session_state.reset_count += 1
    st.rerun()

weeks = sorted(df["주시작일"].unique()) if not df.empty else []
if weeks:
    selected_period = st.sidebar.select_slider(
        "기간",
        options=weeks,
        value=(weeks[0], weeks[-1]),
        key=f"period_{rc}"
    )
else:
    selected_period = (None, None)

platforms = sorted(df["플랫폼"].unique()) if not df.empty else []
selected_platforms = st.sidebar.multiselect(
    "플랫폼",
    options=platforms,
    default=platforms,
    key=f"platforms_{rc}"
)

traffic_sources = sorted(df["유입경로"].unique()) if not df.empty else []
selected_traffic_sources = st.sidebar.multiselect(
    "유입 경로",
    options=traffic_sources,
    default=traffic_sources,
    key=f"traffic_sources_{rc}"
)

# 평소 범위 기준 기간 선택 슬라이더 (고급 설정 expander 안)
default_baseline_end_idx = min(25, len(weeks) - 1) if weeks else 0
with st.sidebar.expander("고급 설정"):
    baseline_period = st.select_slider(
        "평소 범위 기준 기간",
        options=weeks,
        value=(weeks[0], weeks[default_baseline_end_idx]) if weeks else (None, None),
        help="문제가 없던 기간을 고르세요. 이 기간의 주별 주 범위를 '평소 범위'로 씁니다.",
        key=f"baseline_{rc}"
    )
    exclude_low_session = st.checkbox(
        "세션 500개 미만 집단 제외 (집단 비교 · 드릴다운)",
        value=False,
        key=f"exclude_low_{rc}"
    )

# ------------------------------------------------------------
# 1. 보고 있는 조건 캡션 생성 (제목 아래 표시용)
# ------------------------------------------------------------
# 기간 표시
if weeks and len(selected_period) == 2 and selected_period[0] == weeks[0] and selected_period[1] == weeks[-1]:
    period_str = "전체"
elif weeks and selected_period[0] and selected_period[1]:
    period_str = f"{selected_period[0]} ~ {selected_period[1]}"
else:
    period_str = "데이터 없음"

# 플랫폼 표시
if len(selected_platforms) == len(platforms):
    platform_str = "전체"
elif len(selected_platforms) == 0:
    platform_str = "선택 없음"
else:
    platform_str = ", ".join(selected_platforms)

# 유입 경로 표시
if len(selected_traffic_sources) == len(traffic_sources):
    traffic_str = "전체"
elif len(selected_traffic_sources) == 0:
    traffic_str = "선택 없음"
else:
    traffic_str = ", ".join(selected_traffic_sources)

st.caption(f"🔎 보고 있는 조건: 데이터 ({data_source_name}) · 기간 ({period_str}) · 플랫폼 ({platform_str}) · 유입 경로 ({traffic_str})")

# 집단 조건(플랫폼·유입경로)만 적용한 데이터 g
g = df[
    (df["플랫폼"].isin(selected_platforms)) &
    (df["유입경로"].isin(selected_traffic_sources))
] if not df.empty else df

# 기존 f는 g에 기간 조건을 더해 만듦
f = g[
    (g["주시작일"] >= selected_period[0]) &
    (g["주시작일"] <= selected_period[1])
] if not g.empty and selected_period[0] and selected_period[1] else g

# ------------------------------------------------------------
# 사이드바 데이터 다운로드 버튼 영역 추가
# ------------------------------------------------------------
st.sidebar.html("<div style='margin-top: 30px;'></div>")
st.sidebar.subheader("데이터 내려받기")

csv_all = df.to_csv(index=False, encoding="utf-8-sig") if not df.empty else ""
csv_filtered = f.to_csv(index=False, encoding="utf-8-sig") if not f.empty else ""

dl_col1, dl_col2 = st.sidebar.columns(2)
with dl_col1:
    st.download_button(
        label="전체 다운로드",
        data=csv_all.encode("utf-8-sig"),
        file_name="전체_데이터.csv",
        mime="text/csv",
        width="stretch"
    )
with dl_col2:
    st.download_button(
        label="부분 다운로드",
        data=csv_filtered.encode("utf-8-sig"),
        file_name="필터적용_데이터.csv",
        mime="text/csv",
        width="stretch"
    )

# ------------------------------------------------------------
# 탭 구성 (개요, 탐색, 액션, 데이터 업로드)
# ------------------------------------------------------------
tab_overview, tab_explore, tab_action, tab_upload = st.tabs(["📊 개요", "🔍 탐색", "✅ 액션", "📤 데이터 업로드"])

with tab_upload:
    st.caption("이 탭에서 볼 수 있는 것: 분석용 app_events.csv 파일 업로드")
    st.file_uploader("app_events.csv 올리기", type="csv", key="upload_file")
    st.info("GitHub에 데이터를 올리지 않고 로컬 파일을 업로드하여 대시보드 분석을 수행할 수 있습니다. 업로드 후 자동으로 반영됩니다.")

if f.empty:
    st.warning("선택한 조건에 해당하는 데이터가 없습니다. 필터를 바꿔 주세요.")
    st.stop()

with tab_overview:
    st.caption("이 탭에서 볼 수 있는 것: 주요 지표(KPI) 요약, 주별 결제 전환율, 세션 퍼널, 유입 경로별 주문 전환율 및 AI 해설")

    # ------------------------------------------------------------
    # 2. KPI 카드
    # ------------------------------------------------------------
    def rate(a, b):
        return a / b * 100 if b else 0

    t = f[["세션수", "결제시작세션", "주문세션"]].sum()
    c1, c2, c3 = st.columns(3)
    c1.metric("세션 수", f"{t['세션수']:,}개", border=True, help="선택한 조건의 전체 세션의 숫자")
    c2.metric("누적 주문 전환율", f"{rate(t['주문세션'], t['세션수']):.1f}%", border=True, help="주문 세션 ÷ 전체 세션")
    c3.metric("결제 단계 전환율", f"{rate(t['주문세션'], t['결제시작세션']):.1f}%", border=True, help="주문 세션 ÷ 결제 시작 세션")

    # ------------------------------------------------------------
    # 규칙 기반 설명 문장 (KPI 카드 바로 아래 st.info 추가)
    # ------------------------------------------------------------
    # 선택된 기간의 주 목록 정렬
    selected_weeks = sorted(f["주시작일"].unique())

    if len(selected_weeks) < 4:
        conversion_msg_1 = "결제 전환율: 비교하려면 선택 기간을 4주 이상 골라 주세요."
        conversion_msg_2 = "주문 전환율: 비교하려면 선택 기간을 4주 이상 골라 주세요."
    else:
        # 이번 값: f의 마지막 4주 합계로 계산한 전환율
        last_4_weeks = selected_weeks[-4:]
        f_last4 = f[f["주시작일"].isin(last_4_weeks)]
        t_last4 = f_last4[["세션수", "결제시작세션", "주문세션"]].sum()
        
        current_pay_rate = rate(t_last4["주문세션"], t_last4["결제시작세션"])
        current_order_rate = rate(t_last4["주문세션"], t_last4["세션수"])
        
        start_week_str = last_4_weeks[0].strftime("%Y-%m-%d")
        period_desc = f"{start_week_str} 주부터 4주"
        
        # 평소 범위: g에서 기준 기간에 해당하는 주별 전환율 계산 (분모 0인 주 제외)
        g_baseline = g[
            (g["주시작일"] >= baseline_period[0]) &
            (g["주시작일"] <= baseline_period[1])
        ]
        
        # 주별로 합산 후 개별 주 전환율 산출
        g_weekly = g_baseline.groupby("주시작일")[["세션수", "결제시작세션", "주문세션"]].sum().reset_index()
        
        pay_rates_base = []
        order_rates_base = []
        
        for _, row in g_weekly.iterrows():
            if row["결제시작세션"] > 0:
                pay_rates_base.append(row["주문세션"] / row["결제시작세션"] * 100)
            if row["세션수"] > 0:
                order_rates_base.append(row["주문세션"] / row["세션수"] * 100)
                
        if not pay_rates_base or not order_rates_base:
            conversion_msg_1 = "결제 전환율: 기준 기간을 이 집단의 데이터가 있는 시기로 옮겨 주세요."
            conversion_msg_2 = "주문 전환율: 기준 기간을 이 집단의 데이터가 있는 시기로 옮겨 주세요."
        else:
            pay_min, pay_max = min(pay_rates_base), max(pay_rates_base)
            order_min, order_max = min(order_rates_base), max(order_rates_base)
            
            # 결제 전환율 평가 문장 생성
            if current_pay_rate < pay_min:
                diff = pay_min - current_pay_rate
                pay_eval = f"평소 범위({pay_min:.1f}% ~ {pay_max:.1f}%)보다 {diff:.1f}%p 낮습니다."
            elif current_pay_rate > pay_max:
                diff = current_pay_rate - pay_max
                pay_eval = f"평소 범위({pay_min:.1f}% ~ {pay_max:.1f}%)보다 {diff:.1f}%p 높습니다."
            else:
                pay_eval = f"평소 범위({pay_min:.1f}% ~ {pay_max:.1f}%) 안에 있습니다."
            conversion_msg_1 = f"결제 전환율 ({period_desc}): 이번 값은 {current_pay_rate:.1f}%로, {pay_eval}"
            
            # 주문 전환율 평가 문장 생성
            if current_order_rate < order_min:
                diff = order_min - current_order_rate
                order_eval = f"평소 범위({order_min:.1f}% ~ {order_max:.1f}%)보다 {diff:.1f}%p 낮습니다."
            elif current_order_rate > order_max:
                diff = current_order_rate - order_max
                order_eval = f"평소 범위({order_min:.1f}% ~ {order_max:.1f}%)보다 {diff:.1f}%p 높습니다."
            else:
                order_eval = f"평소 범위({order_min:.1f}% ~ {order_max:.1f}%) 안에 있습니다."
            conversion_msg_2 = f"주문 전환율 ({period_desc}): 이번 값은 {current_order_rate:.1f}%로, {order_eval}"

    # ------------------------------------------------------------
    # 규칙 기반 설명 문장 및 AI 해설 통합 컨테이너
    # ------------------------------------------------------------
    with st.container(border=True):
        st.markdown("**📏 지금 숫자 읽기**")
        st.markdown(f"- {conversion_msg_1}")
        st.markdown(f"- {conversion_msg_2}")
        st.caption(f"📏 자동 계산 · 평소 범위 기준 기간 ({baseline_period[0]}) ~ ({baseline_period[1]}) (주별 값)")
        
        with st.expander("🤖 AI 해설 · 전체", expanded=False):
            st.write(AI_NOTES.get("전체", "이 집단의 해설은 아직 없습니다."))
        st.caption(AI_SOURCE)

    st.divider()

    # ------------------------------------------------------------
    # 3. 차트
    # ------------------------------------------------------------
    left, right = st.columns(2, border=True)

    # 3-1. 주별 결제 전환율 (플랫폼별)
    wp = f.groupby(["주시작일", "플랫폼"])[["결제시작세션", "주문세션"]].sum().reset_index()
    wp["결제전환율"] = (wp["주문세션"] / wp["결제시작세션"] * 100).round(1)
    fig1 = px.line(wp, x="주시작일", y="결제전환율", color="플랫폼", markers=True,
                   title="주별 결제 전환율 (플랫폼별)")
    fig1.update_layout(
        yaxis_title="결제 전환율 (%)",
        xaxis_title="주 시작일",
        legend_title="플랫폼",
        xaxis=dict(tickformat="%Y-%m-%d")
    )
    left.plotly_chart(fig1, width="stretch")

    # 3-2. 세션 퍼널
    steps = {"1.방문": "세션수", "2.식당조회": "조회세션", "3.장바구니": "장바구니세션",
             "4.결제시작": "결제시작세션", "5.주문완료": "주문세션"}
    fun = pd.DataFrame({"단계": list(steps), "세션수 (개)": [f[c].sum() for c in steps.values()]})
    fig2 = px.funnel(fun, x="세션수 (개)", y="단계", title="세션 퍼널")
    fig2.update_layout(xaxis_title="세션 수 (개)", yaxis_title="퍼널 단계")
    right.plotly_chart(fig2, width="stretch")

    # 3-3. 유입 경로별 주문 전환율
    ws = f.groupby("유입경로")[["세션수", "주문세션"]].sum().reset_index()
    ws["주문전환율 (%)"] = (ws["주문세션"] / ws["세션수"] * 100).round(1)
    ws = ws.sort_values("주문전환율 (%)")
    fig3 = px.bar(ws, x="주문전환율 (%)", y="유입경로", orientation="h", text_auto=".1f",
                  hover_data={"세션수": True, "주문전환율 (%)": True}, title="유입 경로별 주문 전환율")
    fig3.update_xaxes(range=[0, ws["주문전환율 (%)"].max() * 1.2] if not ws.empty and ws["주문전환율 (%)"].max() > 0 else [0, 100])
    fig3.update_layout(xaxis_title="주문 전환율 (%)", yaxis_title="유입 경로")
    fig3.update_traces(marker_color=[ACCENT if s == "push" else GRAY for s in ws["유입경로"]])
    fig3.update_xaxes(visible=False)
    st.plotly_chart(fig3, width="stretch") 


with tab_explore:
    st.caption("이 탭에서 볼 수 있는 것: 집단 비교(플랫폼 및 유입 경로별 퍼널 비교) 및 드릴다운 분석을 통한 원인 탐색")

    # ------------------------------------------------------------
    # 4. 집단 비교 영역
    # ------------------------------------------------------------
    st.header("집단 비교")
    group_col_choice = st.radio("비교 기준", ["플랫폼", "유입경로"], horizontal=True)

    # 선택된 기준별로 5단계 퍼널 데이터 집계
    group_df = f.groupby(group_col_choice)[["세션수", "조회세션", "장바구니세션", "결제시작세션", "주문세션"]].sum().reset_index()

    # 세션 수가 500개 미만인 집단 확인
    low_session_groups = group_df[group_df["세션수"] < 500][group_col_choice].tolist()
    if low_session_groups:
        st.caption(f"⚠️ 세션 수가 500개 미만인 집단({', '.join(low_session_groups)})이 있어 값이 크게 흔들릴 수 있습니다.")

    # 고급 설정 체크에 따른 집단 필터링 처리
    if exclude_low_session and low_session_groups:
        group_df = group_df[~group_df[group_col_choice].isin(low_session_groups)]
        st.caption(f"제외한 집단: {', '.join(low_session_groups)} (세션 500개 미만)")

    # 시각화용 데이터 프레임 변환 (melt)
    melted_df = group_df.melt(
        id_vars=[group_col_choice],
        value_vars=["세션수", "조회세션", "장바구니세션", "결제시작세션", "주문세션"],
        var_name="단계_원래명",
        value_name="집단세션수"
    )

    step_mapping = {
        "세션수": "1.방문",
        "조회세션": "2.식당조회",
        "장바구니세션": "3.장바구니",
        "결제시작세션": "4.결제시작",
        "주문세션": "5.주문완료"
    }
    melted_df["단계"] = melted_df["단계_원래명"].map(step_mapping)

    # 단계 순서 정렬을 위한 카테고리 설정
    step_order = ["1.방문", "2.식당조회", "3.장바구니", "4.결제시작", "5.주문완료"]
    melted_df["단계"] = pd.Categorical(melted_df["단계"], categories=step_order, ordered=True)
    melted_df = melted_df.sort_values([group_col_choice, "단계"])

    # 누적 전환율(%) 계산 (첫 단계 대비 %)
    melted_df["방문세션수"] = melted_df.groupby(group_col_choice)["집단세션수"].transform(lambda x: x.iloc[0])
    melted_df["누적 전환율 (%)"] = (melted_df["집단세션수"] / melted_df["방문세션수"] * 100).round(1)

    # 단계별 전환율(직전 단계 대비 %) 계산
    melted_df["직전세션수"] = melted_df.groupby(group_col_choice)["집단세션수"].shift(1)
    melted_df["단계 전환율 (%)"] = (melted_df["집단세션수"] / melted_df["직전세션수"] * 100).round(1)
    melted_df.loc[melted_df["단계"] == "1.방문", "단계 전환율 (%)"] = 100.0
    melted_df.loc[melted_df["단계"] == "1.방문", "직전세션수"] = melted_df.loc[melted_df["단계"] == "1.방문", "집단세션수"]

    comp_left, comp_right = st.columns(2)

    # 왼쪽: 누적 전환율 퍼널 (px.funnel, x값이 누적 전환율(%))
    with comp_left:
        fig_comp_funnel = px.funnel(
            melted_df,
            x="누적 전환율 (%)",
            y="단계",
            color=group_col_choice,
            title="누적 전환율 퍼널 (첫 단계 대비 %)"
        )
        fig_comp_funnel.update_layout(xaxis_title="누적 전환율 (%)", yaxis_title="퍼널 단계", legend_title=group_col_choice)
        st.plotly_chart(fig_comp_funnel, width="stretch")

    # 오른쪽: 단계 전환율 막대 (직전 단계 대비 %, barmode="group")
    with comp_right:
        fig_comp_bar = px.bar(
            melted_df,
            x="단계",
            y="단계 전환율 (%)",
            color=group_col_choice,
            barmode="group",
            text="단계 전환율 (%)",
            hover_data={"직전세션수": True, "집단세션수": True, "단계 전환율 (%)": True},
            title="단계 전환율 (직전 단계 대비 %)"
        )
        fig_comp_bar.update_traces(texttemplate='%{text:.1f}%', textposition='outside')
        fig_comp_bar.update_layout(yaxis_title="단계 전환율 (%)", xaxis_title="퍼널 단계", legend_title=group_col_choice)
        st.plotly_chart(fig_comp_bar, width="stretch")

    st.divider()

    # ------------------------------------------------------------
    # 5. 드릴다운: 언제부터, 무엇 때문일까 영역
    # ------------------------------------------------------------
    st.header("드릴다운: 언제부터, 무엇 때문일까")

    # 고른 축(group_col_choice)에 따라 f에 존재하는 값 목록 추출 (고급 설정에서 제외된 집단 반영)
    if exclude_low_session and low_session_groups:
        available_groups = sorted([g_val for g_val in f[group_col_choice].unique() if g_val not in low_session_groups])
    else:
        available_groups = sorted(f[group_col_choice].unique())

    if not available_groups:
        st.warning("선택 가능한 집단이 없습니다. 필터 또는 고급 설정 조건을 확인해 주세요.")
    else:
        drill_col1, drill_col2 = st.columns(2)
        with drill_col1:
            selected_group = st.selectbox("자세히 볼 집단", options=available_groups)
        with drill_col2:
            selected_step_name = st.selectbox(
                "단계",
                options=["방문 → 식당조회", "식당조회 → 장바구니", "장바구니 → 결제시작", "결제시작 → 주문"]
            )

        # 단계별 컬럼 매핑 (직전 단계, 현재 단계)
        step_column_mapping = {
            "방문 → 식당조회": ("세션수", "조회세션"),
            "식당조회 → 장바구니": ("조회세션", "장바구니세션"),
            "장바구니 → 결제시작": ("장바구니세션", "결제시작세션"),
            "결제시작 → 주문": ("결제시작세션", "주문세션")
        }
        prev_col, curr_col = step_column_mapping[selected_step_name]

        # 다른 축 설정 (플랫폼이면 유입경로, 유입경로면 플랫폼)
        other_col = "유입경로" if group_col_choice == "플랫폼" else "플랫폼"

        # 주별 × 다른 축별로 집계
        weekly_drill = f.groupby(["주시작일", group_col_choice, other_col])[[prev_col, curr_col]].sum().reset_index()

        # 선택한 집단만 필터링
        weekly_drill_filtered = weekly_drill[weekly_drill[group_col_choice] == selected_group].copy()

        # 전환율 계산 (직전 단계 세션이 0인 주는 NaN 처리하여 선이 이어지지 않거나 빈 값으로 두도록 함)
        weekly_drill_filtered["전환율 (%)"] = weekly_drill_filtered.apply(
            lambda row: (row[curr_col] / row[prev_col] * 100) if row[prev_col] > 0 else float('nan'),
            axis=1
        )

        # 차트 ① (위): 고른 집단만 남긴 뒤, 다른 축으로 나눠서 주별 전환율 선 차트
        fig_drill_line = px.line(
            weekly_drill_filtered,
            x="주시작일",
            y="전환율 (%)",
            color=other_col,
            markers=True,
            title=f"① {selected_group}의 주별 {selected_step_name} 전환율 — {other_col}별로 쪼개 보기"
        )
        fig_drill_line.update_layout(
            yaxis_title="전환율 (%)",
            xaxis_title="주 시작일",
            legend_title=other_col,
            xaxis=dict(tickformat="%Y-%m-%d")
        )
        st.plotly_chart(fig_drill_line, width="stretch")
        st.caption("모든 갈래가 같은 시점에 함께 움직였다면, 그 축은 원인이 아닙니다.")

        # 차트 ② (아래): 결제 실패 건수 주별 막대 (고른 집단 진한 빨간색 vs 그 외 회색 하나로 합침)
        # 전체 f 데이터에서 주별로 '고른 집단'과 '그 외 집단'의 결제실패 합산 계산
        fail_df = f.groupby(["주시작일", group_col_choice])["결제실패"].sum().reset_index()
        fail_df["구분"] = fail_df[group_col_choice].apply(
            lambda x: selected_group if x == selected_group else "그 외 집단"
        )

        # 주별, 구분별로 다시 합산 (그 외 집단들은 하나로 묶임)
        fail_agg = fail_df.groupby(["주시작일", "구분"])["결제실패"].sum().reset_index()

        # 색상 매핑: 고른 집단은 진한 빨간색(#D32F2F), 그 외 집단은 회색(#9E9E9E)
        color_map = {
            selected_group: "#D32F2F",
            "그 외 집단": "#9E9E9E"
        }

        fig_drill_bar = px.bar(
            fail_agg,
            x="주시작일",
            y="결제실패",
            color="구분",
            barmode="group",
            color_discrete_map=color_map,
            title=f"주별 결제 실패 건수 ({selected_group} vs 그 외 집단)"
        )
        fig_drill_bar.update_layout(
            yaxis_title="결제 실패 건수 (건)",
            xaxis_title="주 시작일",
            legend_title="집단 구분",
            xaxis=dict(tickformat="%Y-%m-%d")
        )
        st.plotly_chart(fig_drill_bar, width="stretch")
        st.caption("문제가 생긴 시점과 함께 움직였다면 원인 후보입니다. 함께 움직였다고 원인이 확정되지는 않습니다.")

    # 2. 드릴다운 차트 아래에 AI 해설 target 추가
    with st.container(border=True):
        with st.expander(f"🤖 AI 해설 · {selected_group if 'selected_group' in locals() and available_groups else '전체'}", expanded=False):
            st.write(AI_NOTES.get(selected_group if 'selected_group' in locals() and available_groups else "전체", "이 집단의 해설은 아직 없습니다."))
        st.caption(AI_SOURCE)


with tab_action:
    st.caption("이 탭에서 볼 수 있는 것: 지표 분석 결과를 바탕으로 도출된 실행 과제 및 액션 플랜")
    st.caption("측정 KPI의 현재 값은 선택한 기간의 마지막 4주로 계산합니다")

    # 사이드바의 기간 필터만 적용하고 플랫폼·유입경로 필터는 무시한 전체 기간 데이터 정의
    f_action_base = df[
        (df["주시작일"] >= selected_period[0]) &
        (df["주시작일"] <= selected_period[1])
    ] if not df.empty and selected_period[0] and selected_period[1] else df

    # 선택 기간의 주 목록 정렬 및 마지막 4주 추출
    action_weeks = sorted(f_action_base["주시작일"].unique()) if not f_action_base.empty else []
    if len(action_weeks) >= 4:
        action_last_4_weeks = action_weeks[-4:]
        f_action_last4 = f_action_base[f_action_base["주시작일"].isin(action_last_4_weeks)]
    else:
        f_action_last4 = f_action_base  # 4주 미만인 경우 전체 선택 기간 사용

    # --- 카드 1 데이터 계산 (플랫폼: Android) ---
    f_card1 = f_action_last4[f_action_last4["플랫폼"] == "Android"] if not f_action_last4.empty else f_action_last4
    t_card1 = f_card1[["결제시작세션", "주문세션"]].sum() if not f_card1.empty else pd.Series({"결제시작세션": 0, "주문세션": 0})
    val_card1 = (t_card1["주문세션"] / t_card1["결제시작세션"] * 100) if t_card1["결제시작세션"] > 0 else None

    # --- 카드 2 데이터 계산 (유입경로: push) ---
    f_card2 = f_action_last4[f_action_last4["유입경로"] == "push"] if not f_action_last4.empty else f_action_last4
    t_card2 = f_card2[["세션수", "조회세션"]].sum() if not f_card2.empty else pd.Series({"세션수": 0, "조회세션": 0})
    val_card2 = (t_card2["조회세션"] / t_card2["세션수"] * 100) if t_card2["세션수"] > 0 else None

    # 카드 2장을 나란히 배치
    action_col1, action_col2 = st.columns(2)

    with action_col1:
        with st.container(border=True):
            st.markdown("**[카드 1]**")
            st.markdown("- **문제**: 결제 시작에서 주문 전환율이 2026-04-13 주부터 하락해 5월 3주차 최저 41.4%, 6월 1주차부터 회복")
            st.markdown("- **근거**: 같은 기간 iOS는 평소 범위 안 · Android 결제 실패가 주 4건 이하 → 최대 153건")
            st.markdown("- **성격**: 특정 시점에 발생한 문제")
            st.markdown("- **액션**: 추가 분석: 앱 버전별로 결제 실패 확인 → 모니터링: 결제 전환율이 평소 범위를 벗어나는지 매주 점검")
            st.markdown("- **우선순위**: 영향 큼 · 실행 쉬움 → 바로 실행")
            
            if val_card1 is not None:
                st.metric("Android 결제 전환율 (최근 4주)", f"{val_card1:.1f}%")
            else:
                st.metric("Android 결제 전환율 (최근 4주)", "데이터 없음")
            
            st.caption("대시보드에서: 개요 탭 규칙 문장(플랫폼 Android) · 탐색 탭 드릴다운")

    with action_col2:
        with st.container(border=True):
            st.markdown("**[카드 2]**")
            st.markdown("- **문제**: 방문 → 식당 조회 전환율 28.4% (다른 경로 57-69%), 주문 전환율 3.2%")
            st.markdown("- **근거**: 2026년 2월부터 월별 세션의 30-36%가 push · 기간 내내 25-35%")
            st.markdown("- **성격**: 전체 구간에 세그먼트별 반응 차이")
            st.markdown("- **액션**: 실험: 일부 사용자에게만 푸시 대상과 문구를 바꿔 방문 → 식당 조회 전환율 비교")
            st.markdown("- **우선순위**: 효과 불확실 · 실행 쉬움 → 실험부터")
            
            if val_card2 is not None:
                st.metric("push 방문 → 식당 조회 전환율 (최근 4주)", f"{val_card2:.1f}%")
            else:
                st.metric("push 방문 → 식당 조회 전환율 (최근 4주)", "데이터 없음")
            
            st.caption("대시보드에서: 탐색 탭 비교 기준 '유입경로' · 드릴다운 push")