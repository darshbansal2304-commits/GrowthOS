import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import json
import re
import sqlite3
from datetime import datetime
from collections import defaultdict
from contextlib import contextmanager
from typing import List, Dict, Any

DB_PATH = "growthos.db"


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS businesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_email TEXT NOT NULL,
                business_name TEXT NOT NULL,
                business_type TEXT,
                city TEXT,
                team_size INTEGER,
                revenue REAL,
                challenge TEXT,
                google_url TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS google_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id INTEGER NOT NULL,
                reviews_json TEXT NOT NULL,
                rating REAL,
                total_reviews INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id INTEGER NOT NULL,
                data_type TEXT NOT NULL,
                month TEXT,
                value REAL,
                notes TEXT,
                raw_text TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_id INTEGER NOT NULL,
                action_text TEXT NOT NULL,
                priority TEXT,
                status TEXT,
                before_value REAL,
                after_value REAL,
                outcome TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)


def add_business(owner_email, name, btype, city, team_size, revenue, challenge, google_url):
    with _db() as conn:
        cursor = conn.execute(
            """INSERT INTO businesses
               (owner_email, business_name, business_type, city, team_size,
                revenue, challenge, google_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_email, name, btype, city, team_size, revenue, challenge, google_url),
        )
        return cursor.lastrowid


def get_business(business_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
        return dict(row) if row else None


def get_businesses_by_owner(owner_email):
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, business_name FROM businesses WHERE owner_email = ? ORDER BY id DESC",
            (owner_email.strip().lower(),),
        ).fetchall()
        return [dict(row) for row in rows]


def check_owner_access(owner_email, business_id):
    business = get_business(business_id)
    return bool(business and business["owner_email"].strip().lower() == owner_email.strip().lower())


def save_google_data(business_id, reviews, rating, total_reviews):
    with _db() as conn:
        conn.execute(
            """INSERT INTO google_data
               (business_id, reviews_json, rating, total_reviews)
               VALUES (?, ?, ?, ?)""",
            (business_id, json.dumps(reviews), rating, total_reviews),
        )


def get_latest_google_data(business_id):
    with _db() as conn:
        row = conn.execute(
            """SELECT * FROM google_data
               WHERE business_id = ? ORDER BY id DESC LIMIT 1""",
            (business_id,),
        ).fetchone()
        return dict(row) if row else None


def save_user_data(business_id, data_type, month, value, notes, raw_text):
    with _db() as conn:
        conn.execute(
            """INSERT INTO user_data
               (business_id, data_type, month, value, notes, raw_text)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (business_id, data_type, month, value, notes, raw_text),
        )


def get_user_data(business_id):
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM user_data
               WHERE business_id = ? ORDER BY month, id""",
            (business_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_full_business_snapshot(business_id):
    business = get_business(business_id)
    google = get_latest_google_data(business_id)
    user_data = get_user_data(business_id)
    return {"business": business, "google": google, "user_data": user_data}


def add_action(business_id, action_text, priority, status):
    with _db() as conn:
        cursor = conn.execute(
            """INSERT INTO actions (business_id, action_text, priority, status)
               VALUES (?, ?, ?, ?)""",
            (business_id, action_text, priority, status),
        )
        return cursor.lastrowid


def update_action_outcome(action_id, before, after, outcome):
    with _db() as conn:
        conn.execute(
            """UPDATE actions SET before_value = ?, after_value = ?, outcome = ?
               WHERE id = ?""",
            (before, after, outcome, action_id),
        )


def get_actions(business_id):
    with _db() as conn:
        rows = conn.execute(
            """SELECT * FROM actions
               WHERE business_id = ? ORDER BY id DESC""",
            (business_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_reviews_for_business(place_id=None, google_url=None, api_key=None):
    return {
        "success": False,
        "error": "Google API not configured. Paste reviews manually instead.",
    }


# =======================================================================
# ANALYSIS ENGINE
# =======================================================================

POSITIVE_WORDS = {
    "good", "great", "excellent", "amazing", "love", "best", "tasty", "delicious",
    "fresh", "clean", "friendly", "helpful", "fast", "quick", "nice", "awesome",
    "wonderful", "perfect", "recommend", "happy", "satisfied", "quality", "yummy"
}

NEGATIVE_WORDS = {
    "bad", "poor", "worst", "terrible", "awful", "slow", "wait", "waiting", "delay",
    "rude", "dirty", "expensive", "overpriced", "stale", "cold", "late", "horrible",
    "disappointing", "waste", "never", "avoid", "problem", "issue", "complaint"
}

THEMES = {
    "Food Quality": {
        "pos": ["tasty", "delicious", "fresh", "yummy", "flavour", "flavor", "quality", "good food"],
        "neg": ["stale", "cold", "bland", "tasteless", "spoiled", "bad taste", "not fresh"]
    },
    "Service": {
        "pos": ["friendly", "helpful", "polite", "courteous", "staff", "service good"],
        "neg": ["rude", "arrogant", "ignored", "slow service", "unhelpful", "attitude"]
    },
    "Waiting Time": {
        "pos": ["fast", "quick", "prompt", "no wait"],
        "neg": ["wait", "waiting", "slow", "queue", "delay", "long time", "took forever"]
    },
    "Pricing": {
        "pos": ["value", "reasonable", "affordable", "worth"],
        "neg": ["expensive", "costly", "overpriced", "pricey", "high price"]
    },
    "Cleanliness": {
        "pos": ["clean", "hygienic", "neat", "tidy"],
        "neg": ["dirty", "messy", "unclean", "hygiene", "filthy"]
    },
    "Ambience": {
        "pos": ["nice place", "good ambience", "comfortable", "cozy", "atmosphere"],
        "neg": ["noisy", "crowded", "uncomfortable", "bad ambience"]
    },
}

IMPROVEMENT_PLAYBOOK = {
    "Waiting Time": [
        "Introduce a simple token or queue system",
        "Display estimated waiting time clearly",
        "Offer water or a small complimentary item while customers wait",
        "Train staff to give time updates every 5–7 minutes",
    ],
    "Service": [
        "Conduct a 15-minute daily briefing on polite language",
        "Create a simple service checklist for every order",
        "Start a 'Staff of the Week' recognition",
        "Add a feedback QR code on every table or bill",
    ],
    "Food Quality": [
        "Do a daily taste test of 2–3 key items before opening",
        "Reduce batch size so food stays fresher",
        "Write exact recipes and photos for consistency",
        "Ask 5 regular customers for specific taste feedback this week",
    ],
    "Pricing": [
        "Review the top 5 selling items for value perception",
        "Create a 'Value Combo' that feels generous",
        "Clearly show portion size and what is included",
        "Test a small price adjustment on one item only",
    ],
    "Cleanliness": [
        "Make a visible cleaning checklist every hour",
        "Assign one person as 'Hygiene Champion' each day",
        "Deep clean one area every night after closing",
        "Add sanitizer and tissue at customer touchpoints",
    ],
    "Ambience": [
        "Fix lighting and music volume",
        "Remove clutter from customer view",
        "Add 2–3 plants or simple decoration",
        "Ensure AC or fan is working properly",
    ],
}


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def simple_sentiment(text: str) -> float:
    words = set(re.findall(r"\b\w+\b", _clean_text(text)))
    pos = len(words & POSITIVE_WORDS)
    neg = len(words & NEGATIVE_WORDS)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def analyze_reviews(reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not reviews:
        return {
            "total_reviews": 0,
            "avg_rating": None,
            "sentiment_score": 0.0,
            "strengths": [],
            "weaknesses": [],
            "improvements": [],
            "theme_stats": {},
            "rating_distribution": {},
        }

    texts = [r.get("text", "") for r in reviews if r.get("text")]
    ratings = [r.get("rating") for r in reviews if r.get("rating") is not None]

    total = len(texts)
    avg_rating = sum(ratings) / len(ratings) if ratings else None
    sentiments = [simple_sentiment(t) for t in texts]
    avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0

    theme_stats = {}
    for theme, kw in THEMES.items():
        pos_count = 0
        neg_count = 0
        evidence_pos = []
        evidence_neg = []

        for t in texts:
            clean = _clean_text(t)
            for w in kw["pos"]:
                if w in clean:
                    pos_count += 1
                    if len(evidence_pos) < 3:
                        evidence_pos.append(t[:130] + ("..." if len(t) > 130 else ""))
                    break
            for w in kw["neg"]:
                if w in clean:
                    neg_count += 1
                    if len(evidence_neg) < 3:
                        evidence_neg.append(t[:130] + ("..." if len(t) > 130 else ""))
                    break

        theme_stats[theme] = {
            "positive_mentions": pos_count,
            "negative_mentions": neg_count,
            "net": pos_count - neg_count,
            "evidence_positive": evidence_pos,
            "evidence_negative": evidence_neg,
        }

    strengths = []
    for theme, stat in theme_stats.items():
        if stat["positive_mentions"] >= 2 and stat["net"] > 0:
            strengths.append({
                "theme": theme,
                "mentions": stat["positive_mentions"],
                "evidence": stat["evidence_positive"],
                "confidence": min(95, 50 + stat["positive_mentions"] * 8),
            })
    strengths = sorted(strengths, key=lambda x: x["mentions"], reverse=True)

    weaknesses = []
    for theme, stat in theme_stats.items():
        if stat["negative_mentions"] >= 2 and stat["net"] < 0:
            severity = "High" if stat["negative_mentions"] >= 5 else "Medium"
            weaknesses.append({
                "theme": theme,
                "mentions": stat["negative_mentions"],
                "severity": severity,
                "evidence": stat["evidence_negative"],
                "confidence": min(95, 50 + stat["negative_mentions"] * 8),
            })
    weaknesses = sorted(weaknesses, key=lambda x: x["mentions"], reverse=True)

    improvements = []
    for w in weaknesses:
        theme = w["theme"]
        techniques = IMPROVEMENT_PLAYBOOK.get(theme, ["Talk to 5 customers this week about this issue"])
        improvements.append({
            "theme": theme,
            "priority": "P1" if w["severity"] == "High" else "P2",
            "problem": f"{w['mentions']} negative mentions of {theme}",
            "techniques": techniques[:3],
            "how_to_measure": f"Count the number of negative '{theme}' mentions in the next 15–20 reviews",
            "evidence_count": w["mentions"],
        })

    rating_dist = defaultdict(int)
    for r in ratings:
        rating_dist[int(r)] += 1

    return {
        "total_reviews": total,
        "avg_rating": round(avg_rating, 2) if avg_rating else None,
        "sentiment_score": round(avg_sentiment, 2),
        "strengths": strengths,
        "weaknesses": weaknesses,
        "improvements": improvements,
        "theme_stats": theme_stats,
        "rating_distribution": dict(rating_dist),
    }


def calculate_health_score(sales: List[float], analysis: Dict[str, Any], repeat_rate: float = 50.0) -> int:
    score = 50.0

    if len(sales) >= 2 and sales[0] != 0:
        trend = (sales[-1] - sales[0]) / sales[0] * 100
        if trend > 15:
            score += 15
        elif trend > 5:
            score += 8
        elif trend < -15:
            score -= 15
        elif trend < -5:
            score -= 8

    total_rev = analysis.get("total_reviews", 0)
    sentiment = analysis.get("sentiment_score", 0)
    if total_rev >= 15:
        score += 5
    if sentiment > 0.3:
        score += 12
    elif sentiment > 0.1:
        score += 6
    elif sentiment < -0.2:
        score -= 12
    elif sentiment < 0:
        score -= 6

    high_weak = sum(1 for w in analysis.get("weaknesses", []) if w["severity"] == "High")
    score -= high_weak * 8

    if repeat_rate >= 60:
        score += 10
    elif repeat_rate < 35:
        score -= 10

    return int(max(0, min(100, round(score))))


# =======================================================================
# APP SETUP
# =======================================================================

st.set_page_config(
    page_title="GrowthOS – Real Evidence",
    layout="wide",
    page_icon="📈",
    initial_sidebar_state="expanded",
)

init_db()


def init_session():
    if "page" not in st.session_state:
        st.session_state.page = "Onboarding"
    if "owner_email" not in st.session_state:
        st.session_state.owner_email = ""
    if "business_id" not in st.session_state:
        st.session_state.business_id = None


def go_to(page: str):
    st.session_state.page = page
    st.rerun()


# =======================================================================
# PAGES
# =======================================================================

def page_onboarding():
    st.title("Create Business Profile")
    st.caption("All data stays private to the owner email you enter.")

    with st.form("onboard_form"):
        owner_email = st.text_input("Owner Email *", value=st.session_state.owner_email or "owner@example.com")
        name = st.text_input("Business Name *", "Sharma Sweets")
        btype = st.selectbox("Business Type", ["restaurant", "salon", "clinic", "retail", "tutor", "other"])
        city = st.text_input("City *", "Fatehpur")
        team = st.number_input("Team Size", 1, 200, 4)
        revenue = st.number_input("Approx Monthly Revenue (₹)", min_value=0, value=50000, step=1000)
        challenge = st.text_area("Biggest Challenge right now")
        google_url = st.text_input("Google Business Profile URL (optional)")

        submitted = st.form_submit_button("Save Business →", type="primary", use_container_width=True)

        if submitted:
            if not owner_email.strip() or not name.strip() or not city.strip():
                st.error("Owner Email, Business Name and City are required.")
                return

            bid = add_business(
                owner_email=owner_email.strip().lower(),
                name=name.strip(),
                btype=btype,
                city=city.strip(),
                team_size=int(team),
                revenue=float(revenue),
                challenge=challenge,
                google_url=google_url,
            )
            st.session_state.owner_email = owner_email.strip().lower()
            st.session_state.business_id = bid
            st.success(f"✅ Business created · ID **{bid}**")
            st.info("Now go to **Connect Data** or **Dashboard** from the sidebar.")


def page_connect():
    st.title("Connect Real Data Sources")
    st.info("You can paste reviews manually and store them in your local database.")

    if not st.session_state.business_id:
        st.warning("Please create a business first (Onboarding).")
        return

    bid = st.session_state.business_id
    owner = st.session_state.owner_email

    if not check_owner_access(owner, bid):
        st.error("Access denied.")
        return

    tab1, tab2 = st.tabs(["Google Reviews", "Sales / Manual Data"])

    with tab1:
        st.subheader("Google Business Reviews")
        col1, col2 = st.columns(2)
        with col1:
            google_url = st.text_input("Paste Google Business URL")
        with col2:
            reviews_paste = st.text_area(
                "Paste real reviews here (one per line or paragraph)",
                height=220,
                placeholder="Waiting time was too long...\nStaff was helpful...\nPrices are high..."
            )

        if st.button("Save Reviews", type="primary"):
            reviews = []
            rating = None
            total = 0

            if reviews_paste.strip():
                lines = [l.strip() for l in reviews_paste.split("\n") if l.strip()]
                reviews = [{"text": t, "rating": None} for t in lines]
                total = len(reviews)
                st.success(f"Saved {len(reviews)} manually pasted reviews")

            if reviews:
                save_google_data(
                    business_id=bid,
                    reviews=reviews,
                    rating=rating,
                    total_reviews=total,
                )
                st.balloons()
            else:
                st.error("No reviews to save. Paste text first.")

    with tab2:
        st.subheader("Add Sales / Feedback / Expenses")
        dtype = st.selectbox("Data Type", ["sales", "feedback", "expense", "other"])
        month = st.text_input("Month (YYYY-MM)", value=datetime.now().strftime("%Y-%m"))
        value = st.number_input("Value (₹)", min_value=0.0, value=0.0, step=500.0)
        notes = st.text_area("Notes / Raw text")

        if st.button("Save Data Point", type="primary"):
            save_user_data(
                business_id=bid,
                data_type=dtype,
                month=month,
                value=value if value > 0 else None,
                notes=notes,
                raw_text=notes,
            )
            st.success("Saved to your private database.")


def page_dashboard():
    st.title("📊 Live Growth Dashboard – Decision Support")

    if not st.session_state.business_id or not st.session_state.owner_email:
        st.warning("Please complete Onboarding first.")
        return

    bid = st.session_state.business_id
    owner = st.session_state.owner_email

    if not check_owner_access(owner, bid):
        st.error("⛔ Access Denied – This business belongs to another owner.")
        st.stop()

    snapshot = get_full_business_snapshot(bid)
    biz = snapshot.get("business")
    google = snapshot.get("google")
    user_rows = snapshot.get("user_data", [])

    if not biz:
        st.error("Business not found.")
        return

    st.caption(f"Owner: {biz['owner_email']} · {biz.get('city')} · {biz.get('business_type', '').title()}")

    sales = []
    for row in user_rows:
        if row.get("data_type") == "sales" and row.get("value") is not None:
            sales.append(float(row["value"]))
    sales = list(reversed(sales))[-8:]

    reviews = []
    if google and google.get("reviews_json"):
        try:
            reviews = json.loads(google["reviews_json"])
        except Exception:
            reviews = []

    for row in user_rows:
        if row.get("data_type") == "feedback" and row.get("raw_text"):
            reviews.append({"text": row["raw_text"], "rating": None})

    analysis = analyze_reviews(reviews)
    health = calculate_health_score(sales, analysis, repeat_rate=50)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Health Score", f"{health}/100", help="Calculated from your real business data")
    m2.metric("Avg Rating", analysis["avg_rating"] if analysis["avg_rating"] else "–")
    m3.metric("Reviews Analysed", analysis["total_reviews"])
    m4.metric("Sentiment", f"{analysis['sentiment_score']:+.2f}",
              help="+1 = very positive, -1 = very negative")

    st.divider()

    g1, g2 = st.columns(2)

    with g1:
        if analysis["rating_distribution"]:
            rdf = pd.DataFrame({
                "Rating": list(analysis["rating_distribution"].keys()),
                "Count": list(analysis["rating_distribution"].values())
            }).sort_values("Rating")
            fig = px.bar(rdf, x="Rating", y="Count", title="⭐ Rating Distribution",
                         color="Count", color_continuous_scale="RdYlGn")
            fig.update_layout(height=340, margin=dict(t=40, b=20))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Add reviews with ratings to see distribution.")

    with g2:
        theme_data = []
        for theme, stat in analysis["theme_stats"].items():
            if stat["positive_mentions"] + stat["negative_mentions"] > 0:
                theme_data.append({
                    "Theme": theme,
                    "Positive": stat["positive_mentions"],
                    "Negative": stat["negative_mentions"]
                })
        if theme_data:
            tdf = pd.DataFrame(theme_data)
            fig = go.Figure()
            fig.add_trace(go.Bar(name="Positive", x=tdf["Theme"], y=tdf["Positive"], marker_color="#27ae60"))
            fig.add_trace(go.Bar(name="Negative", x=tdf["Theme"], y=tdf["Negative"], marker_color="#e74c3c"))
            fig.update_layout(barmode="group", title="Theme Mentions (Positive vs Negative)", height=340,
                              margin=dict(t=40, b=20))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Not enough theme signals yet.")

    if sales:
        st.subheader("Sales Trend (from your data)")
        fig_sales = px.line(
            x=[f"P{i+1}" for i in range(len(sales))],
            y=sales,
            markers=True,
            title="Real Sales Points"
        )
        fig_sales.update_layout(height=280, margin=dict(t=40, b=20))
        st.plotly_chart(fig_sales, use_container_width=True)

    st.divider()

    st.subheader("✅ Strengths (Verified from real reviews)")
    if analysis["strengths"]:
        for s in analysis["strengths"]:
            with st.expander(f"**{s['theme']}** · {s['mentions']} positive mentions · Confidence {s['confidence']}%"):
                st.write("**Evidence (real review excerpts):**")
                for e in s["evidence"]:
                    st.markdown(f"- _{e}_")
    else:
        st.info("Not enough clear positive patterns yet. Collect more reviews.")

    st.subheader("⚠️ Weaknesses (Verified from real reviews)")
    if analysis["weaknesses"]:
        for w in analysis["weaknesses"]:
            severity_icon = "🔴" if w["severity"] == "High" else "🟠"
            with st.expander(f"{severity_icon} **{w['theme']}** · {w['mentions']} negative mentions · {w['severity']} severity"):
                st.write("**Evidence (real review excerpts):**")
                for e in w["evidence"]:
                    st.markdown(f"- _{e}_")
    else:
        st.success("No major weaknesses detected with current data volume.")

    st.divider()

    st.subheader("🛠️ Prioritized Improvement Techniques (Decision Support)")
    if analysis["improvements"]:
        for imp in analysis["improvements"]:
            st.markdown(f"### {imp['priority']} – Fix **{imp['theme']}**")
            st.caption(imp["problem"])
            st.write("**What the owner should do this week:**")
            for i, tech in enumerate(imp["techniques"], 1):
                st.markdown(f"{i}. {tech}")
            st.info(f"**How to measure if it worked:** {imp['how_to_measure']}")
            st.divider()
    else:
        st.info("No high-priority improvements needed right now. Keep collecting reviews.")


def page_actions():
    st.title("🔄 Action Tracker – Before vs After")

    if not st.session_state.business_id:
        st.warning("Create a business first.")
        return

    bid = st.session_state.business_id
    if not check_owner_access(st.session_state.owner_email, bid):
        st.error("Access denied.")
        return

    with st.form("action_form"):
        action_text = st.text_input("What action did you take?", placeholder="Started token system for waiting customers")
        c1, c2, c3 = st.columns(3)
        before = c1.number_input("Before value (₹ or metric)", value=0.0, step=100.0)
        after = c2.number_input("After value (real measurement)", value=0.0, step=100.0)
        outcome = c3.selectbox("Outcome", ["Improved", "No Change", "Worsened"])

        if st.form_submit_button("Save Measurement", type="primary"):
            if not action_text.strip():
                st.error("Please describe the action.")
            else:
                aid = add_action(business_id=bid, action_text=action_text, priority="P1", status="DONE")
                update_action_outcome(aid, before, after, outcome)
                st.success("Saved to your private action history.")

    st.subheader("History of Real Measurements")
    rows = get_actions(bid)
    if rows:
        df = pd.DataFrame([dict(r) for r in rows])
        show_cols = [c for c in ["id", "action_text", "before_value", "after_value", "outcome", "created_at"] if c in df.columns]
        st.dataframe(df[show_cols], use_container_width=True, hide_index=True)
    else:
        st.info("No actions recorded yet.")


# =======================================================================
# MAIN
# =======================================================================

def main():
    init_session()

    with st.sidebar:
        st.title("📈 GrowthOS")
        st.caption("Evidence → Decision → Action → Outcome")

        email_input = st.text_input(
            "Owner Email",
            value=st.session_state.owner_email or "",
            key="sidebar_email",
        )
        st.session_state.owner_email = email_input.strip().lower()

        if st.session_state.owner_email:
            businesses = get_businesses_by_owner(st.session_state.owner_email)
            if businesses:
                st.subheader("Your Businesses")
                for b in businesses:
                    if st.button(f"{b['business_name']} (ID {b['id']})", key=f"biz_{b['id']}"):
                        st.session_state.business_id = b["id"]
                        go_to("Dashboard")

            st.divider()
            st.write("**Navigate**")
            if st.button("🏠 Onboarding", use_container_width=True):
                go_to("Onboarding")
            if st.button("🔗 Connect Data", use_container_width=True):
                go_to("Connect")
            if st.button("📊 Dashboard", use_container_width=True):
                go_to("Dashboard")
            if st.button("🔄 Action Tracker", use_container_width=True):
                go_to("Actions")

            if st.session_state.business_id:
                st.caption(f"Active Business ID: {st.session_state.business_id}")

    # Page routing
    page = st.session_state.page
    if page == "Onboarding":
        page_onboarding()
    elif page == "Connect":
        page_connect()
    elif page == "Dashboard":
        page_dashboard()
    elif page == "Actions":
        page_actions()
    else:
        page_onboarding()


if __name__ == "__main__":
    main()
