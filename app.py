from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from datetime import date, datetime, timedelta
from functools import wraps
import smtplib
from email.mime.text import MIMEText
import json
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'tjw-dev-secret')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

database_url = os.environ.get('DATABASE_URL', 'sqlite:///tracker.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

COACHES  = ['Shruti', 'Veena', 'Poushali', 'Anindya', 'Swati', 'Ajay']

# Weighted compliance — must sum to 1.0
WEIGHTS = {
    'applications': 0.30,
    'outreach':     0.25,
    'follow_ups':   0.20,
    'posts':        0.15,
    'comments':     0.10,
}
FOUNDERS = ['Arindam', 'Poulomi']
MANAGERS = ['Poushali']  # Cross-coach access for content & engagement
ALERT_EMAIL = 'athejobworkshop@gmail.com'

DEFAULT_BASELINES = [
    {'activity': 'comments',     'label': 'Comments',     'value': 25, 'suggested': 25},
    {'activity': 'posts',        'label': 'Posts',        'value': 3,  'suggested': 3},
    {'activity': 'outreach',     'label': 'Outreach',     'value': 50, 'suggested': 50},
    {'activity': 'applications', 'label': 'Applications', 'value': 18, 'suggested': 18},
]


# ── Auth ──────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'name' not in session:
            flash('Please select your name to continue.', 'warning')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def founder_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'founder':
            flash('Founder access required.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def is_manager():
    return session.get('name') in MANAGERS

def can_write_client(client):
    if session.get('role') == 'founder':
        return True
    if is_manager():
        return True
    return session.get('name') == client.coach_name


@app.context_processor
def inject_session_info():
    return {
        'current_user': session.get('name'),
        'current_role': session.get('role'),
        'is_founder':   session.get('role') == 'founder',
        'is_manager':   is_manager(),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def week_monday(d=None):
    if d is None:
        d = date.today()
    return d - timedelta(days=d.weekday())


def week_friday(monday):
    return monday + timedelta(days=5)  # Saturday


def get_weeks_for_client(client):
    weeks, current = [], week_monday(client.start_date)
    this_monday = week_monday()
    while current <= this_monday:
        weeks.append((current, week_friday(current)))
        current += timedelta(weeks=1)
    return weeks


def activity_status(actual, target):
    if target == 0: return 'success'
    if actual >= target: return 'success'
    if actual >= target * 0.7: return 'warning'
    return 'danger'


def pct(actual, target):
    if target == 0: return 0.0
    return min(100.0, round(actual / target * 100, 1))


# ── Models ────────────────────────────────────────────────────────────────────

class Baseline(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    activity  = db.Column(db.String(50), unique=True, nullable=False)
    label     = db.Column(db.String(100), nullable=False)
    value     = db.Column(db.Integer, nullable=False)
    suggested = db.Column(db.Integer, nullable=False)


class Client(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    coach_name    = db.Column(db.String(50),  nullable=False)
    start_date    = db.Column(db.Date, nullable=False, default=date.today)
    end_date      = db.Column(db.Date, nullable=True)
    active        = db.Column(db.Boolean, default=True)
    module_1_done = db.Column(db.Boolean, default=False)
    module_2_done = db.Column(db.Boolean, default=False)
    module_3_done = db.Column(db.Boolean, default=False)
    logs             = db.relationship('WeeklyLog',  backref='client', lazy=True,
                                       order_by='WeeklyLog.week_start.desc()')
    client_baselines = db.relationship('ClientBaseline', backref='client',
                                       lazy=True, cascade='all, delete-orphan')
    sent_reports     = db.relationship('ReportSent', backref='client',
                                       lazy=True, order_by='ReportSent.sent_at.desc()')

    def get_baselines(self):
        if self.client_baselines:
            return {b.activity: b.value for b in self.client_baselines}
        return {b.activity: b.value for b in Baseline.query.order_by(Baseline.id).all()}

    def get_week_log(self, monday):
        return WeeklyLog.query.filter_by(client_id=self.id, week_start=monday).first()

    def current_week_log(self):
        return self.get_week_log(week_monday())

    def weeks_progress(self):
        baselines = self.get_baselines()
        result = []
        for ws, we in get_weeks_for_client(self):
            log = self.get_week_log(ws)
            result.append({
                'week_start': ws, 'week_end': we, 'log': log,
                'compliance': log.overall_pct(baselines) if log else None,
                'statuses':   log.statuses(baselines)    if log else None,
            })
        result.reverse()
        return result


class ClientBaseline(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False)
    activity  = db.Column(db.String(50),  nullable=False)
    label     = db.Column(db.String(100), nullable=False)
    value     = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint('client_id', 'activity', name='uq_cb_ca'),)


class WeeklyLog(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    client_id    = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False)
    week_start   = db.Column(db.Date, nullable=False)
    comments     = db.Column(db.Integer, default=0)
    posts        = db.Column(db.Integer, default=0)
    outreach     = db.Column(db.Integer, default=0)
    applications = db.Column(db.Integer, default=0)
    follow_ups   = db.Column(db.Integer, default=0)
    notes        = db.Column(db.Text, default='')
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('client_id', 'week_start', name='uq_wl_cw'),)

    def week_end(self):       return self.week_start + timedelta(days=5)  # Saturday
    def follow_up_target(self): return self.applications * 2

    def compliance_pcts(self, baselines):
        fut = self.follow_up_target()
        return {
            'comments':     pct(self.comments,    baselines['comments']),
            'posts':        pct(self.posts,        baselines['posts']),
            'outreach':     pct(self.outreach,     baselines['outreach']),
            'applications': pct(self.applications, baselines['applications']),
            'follow_ups':   pct(self.follow_ups, fut) if fut > 0 else 100.0,
        }

    def overall_pct(self, baselines):
        p = self.compliance_pcts(baselines)
        return round(sum(p[k] * w for k, w in WEIGHTS.items()), 1)

    def statuses(self, baselines):
        return {
            'comments':     activity_status(self.comments,    baselines['comments']),
            'posts':        activity_status(self.posts,        baselines['posts']),
            'outreach':     activity_status(self.outreach,     baselines['outreach']),
            'applications': activity_status(self.applications, baselines['applications']),
            'follow_ups':   activity_status(self.follow_ups,   self.follow_up_target()),
        }


class CoachSession(db.Model):
    """One row every time someone selects their name."""
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(50), nullable=False)
    role         = db.Column(db.String(20), nullable=False)
    logged_in_at = db.Column(db.DateTime,   nullable=False, default=datetime.utcnow)


class ReportSent(db.Model):
    """Frozen snapshot of a report at the moment it was marked as sent."""
    id          = db.Column(db.Integer, primary_key=True)
    client_id   = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False)
    sent_by     = db.Column(db.String(50), nullable=False)
    sent_at     = db.Column(db.DateTime,   nullable=False, default=datetime.utcnow)
    from_week   = db.Column(db.Date, nullable=False)
    to_week     = db.Column(db.Date, nullable=False)
    report_json = db.Column(db.Text, nullable=False)


class AuditLog(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    actor     = db.Column(db.String(50), nullable=False)
    action    = db.Column(db.String(50), nullable=False)
    entity    = db.Column(db.String(100), nullable=False)
    detail    = db.Column(db.Text, default='')


def audit(action, entity, detail=''):
    db.session.add(AuditLog(
        actor=session.get('name', 'system'),
        action=action,
        entity=entity,
        detail=detail,
    ))


# ── Email ─────────────────────────────────────────────────────────────────────

def send_alert(coach_name, client_name, ws, low):
    user = os.environ.get('MAIL_USERNAME')
    pwd  = os.environ.get('MAIL_PASSWORD')
    if not user or not pwd:
        return
    we = week_friday(ws)
    subject = f"⚠️ Low Compliance — {coach_name} / {client_name} ({ws.strftime('%d %b')})"
    lines = [
        f"Low compliance detected\n",
        f"Coach:  {coach_name}",
        f"Client: {client_name}",
        f"Week:   {ws.strftime('%d %b')} – {we.strftime('%d %b %Y')}\n",
        "Activities below 70% of target:",
    ]
    for a in low:
        lines.append(f"  • {a['label']}: {a['actual']}/{a['target']} ({a['pct']:.0f}%)")
    lines.append("\nPlease review on the TJW Progress Tracker dashboard.")
    msg = MIMEText("\n".join(lines), 'plain')
    msg['Subject'] = subject
    msg['From']    = user
    msg['To']      = ALERT_EMAIL
    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as s:
            s.starttls()
            s.login(user, pwd)
            s.sendmail(user, ALERT_EMAIL, msg.as_string())
    except Exception:
        pass


# ── Report helpers ────────────────────────────────────────────────────────────

def generate_summary(client_name, averages):
    if not averages:
        return (f"No activity has been logged for this period yet. "
                f"Once the week is recorded, a progress summary will appear here.")
    overall = averages['overall']
    acts = {
        'commenting on posts': averages['comments'],
        'publishing posts':    averages['posts'],
        'outreach':            averages['outreach'],
        'job applications':    averages['applications'],
        'follow-ups':          averages['follow_ups'],
    }
    best_key  = max(acts, key=acts.get)
    worst_key = min(acts, key=acts.get)
    worst_val = acts[worst_key]

    if overall >= 90:
        opening = (f"Really strong week, {client_name}. The consistency across activities "
                   f"is exactly what builds momentum and the numbers reflect that.")
    elif overall >= 70:
        opening = (f"Good effort this week, {client_name}. You are on track overall "
                   f"and making solid progress. Keep showing up with the same energy.")
    elif overall >= 50:
        opening = (f"A mixed week for {client_name}. There were some bright spots "
                   f"but also areas where things did not quite land. That is okay. "
                   f"The goal is to learn from it and come back stronger.")
    else:
        opening = (f"This was a tough week for {client_name}, and that happens. "
                   f"The important thing is to identify what got in the way "
                   f"and make a clear plan for next week.")

    strength = (f"The standout area was {best_key}, where the effort was clearly visible "
                f"and the activity level was well maintained.")

    if worst_val < 70:
        improvement = (f"The one area to prioritise next week is {worst_key}. "
                       f"Even a small, consistent push here will make a noticeable "
                       f"difference to the overall picture.")
    else:
        improvement = (f"All areas are in a good place right now. "
                       f"The focus for next week should be on keeping this consistency "
                       f"rather than letting any single area slip.")

    return f"{opening} {strength} {improvement}"


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'name' in session:
        if session.get('role') == 'founder':
            return redirect(url_for('dashboard'))
        return redirect(url_for('coach', name=session['name']))

    h = datetime.now().hour
    greeting = 'morning' if h < 12 else 'afternoon' if h < 17 else 'evening'

    # Build compliance data for public founder dashboard
    coach_param    = request.args.get('coach', '')
    week_param     = request.args.get('week', '')
    activity_param = request.args.get('activity', 'overall')

    # Generate last 9 Mondays (current + 8 prior)
    week_options = []
    base = week_monday()
    for i in range(9):
        m = base - timedelta(weeks=i)
        week_options.append(m)

    # Determine selected week
    try:
        selected_week = date.fromisoformat(week_param)
        selected_week = week_monday(selected_week)
    except Exception:
        selected_week = week_options[0]

    # Compute per-coach compliance rows
    activity_map = {
        'overall':      'Overall',
        'comments':     'Comments',
        'posts':        'Posts',
        'outreach':     'Outreach',
        'applications': 'Applications',
        'follow_ups':   'Follow-ups',
    }

    founder_rows = []
    for cname in COACHES:
        if coach_param and coach_param != cname:
            continue
        clients = Client.query.filter_by(coach_name=cname, active=True).all()
        total   = len(clients)
        logged  = 0
        acts    = ['comments', 'posts', 'outreach', 'applications', 'follow_ups']
        actual  = {a: 0 for a in acts}
        target  = {a: 0 for a in acts}
        for c in clients:
            bl  = c.get_baselines()
            log = c.get_week_log(selected_week)
            if log:
                logged += 1
                actual['comments']     += log.comments
                actual['posts']        += log.posts
                actual['outreach']     += log.outreach
                actual['applications'] += log.applications
                actual['follow_ups']   += log.follow_ups
                target['follow_ups']   += log.follow_up_target()
            for a in ['comments', 'posts', 'outreach', 'applications']:
                target[a] += bl[a]
        _p = lambda a: pct(actual[a], target[a]) if target[a] else 0.0
        act_pcts = {a: _p(a) for a in acts}
        act_pcts['overall'] = round(sum(act_pcts[k] * w for k, w in WEIGHTS.items()), 1)

        if activity_param in act_pcts:
            metric_val = act_pcts[activity_param]
        else:
            metric_val = act_pcts['overall']

        founder_rows.append({
            'name':       cname,
            'total':      total,
            'logged':     logged,
            'metric_val': metric_val,
            'act_pcts':   act_pcts,
        })

    # Top/bottom comparison text
    comparison = ''
    if len(founder_rows) >= 2:
        sorted_rows = sorted(founder_rows, key=lambda r: r['metric_val'], reverse=True)
        top    = sorted_rows[0]
        bottom = sorted_rows[-1]
        if top['name'] != bottom['name']:
            act_label = activity_map.get(activity_param, 'Overall')
            comparison = (
                f"{top['name']} leads on {act_label} at {top['metric_val']:.0f}%, "
                f"compared to {bottom['name']} at {bottom['metric_val']:.0f}%."
            )

    founder_data = {
        'rows':           founder_rows,
        'week_options':   week_options,
        'selected_week':  selected_week,
        'coach_param':    coach_param,
        'activity_param': activity_param,
        'activity_map':   activity_map,
        'comparison':     comparison,
    }

    return render_template('index.html', coaches=COACHES, founders=FOUNDERS,
                           greeting=greeting, founder_data=founder_data)


@app.route('/select_name', methods=['POST'])
def select_name():
    name = request.form.get('name', '')
    if name not in COACHES:
        return redirect(url_for('index'))
    session.clear()
    session['name'] = name
    session['role'] = 'coach'
    db.session.add(CoachSession(name=name, role='coach'))
    db.session.commit()
    return redirect(url_for('coach', name=name))


@app.route('/founder/login', methods=['POST'])
def founder_login():
    name = request.form.get('name', '')
    pwd  = request.form.get('password', '')
    if name in FOUNDERS and pwd == os.environ.get('FOUNDER_PASSWORD', 'tjw-founder-2026'):
        session.clear()
        session['name'] = name
        session['role'] = 'founder'
        db.session.add(CoachSession(name=name, role='founder'))
        db.session.commit()
        return redirect(url_for('dashboard'))
    flash('Incorrect password.', 'danger')
    return redirect(url_for('index'))


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('index'))


# ── Coach ─────────────────────────────────────────────────────────────────────

@app.route('/coach/<name>')
@login_required
def coach(name):
    if name not in COACHES:
        return redirect(url_for('index'))
    if session['role'] == 'coach' and session['name'] != name and not is_manager():
        return redirect(url_for('coach', name=session['name']))
    clients = (Client.query.filter_by(coach_name=name, active=True)
               .order_by(Client.name).all())
    this_week = week_monday()
    cards = []
    for c in clients:
        wks = get_weeks_for_client(c)
        cards.append({
            'client':       c,
            'total_weeks':  len(wks),
            'logged_weeks': sum(1 for ws, _ in wks if c.get_week_log(ws)),
            'this_log':     c.current_week_log(),
            'baselines':    c.get_baselines(),
            'can_write':    can_write_client(c),
        })
    return render_template('coach.html', coach_name=name, client_cards=cards,
                           this_week=this_week, this_week_end=week_friday(this_week))


@app.route('/coach/<name>/add_client', methods=['GET', 'POST'])
@login_required
def add_client(name):
    if name not in COACHES:
        return redirect(url_for('index'))
    if session['role'] == 'coach' and session['name'] != name and not is_manager():
        return redirect(url_for('coach', name=session['name']))
    global_bl = Baseline.query.order_by(Baseline.id).all()
    if request.method == 'POST':
        cname  = request.form.get('client_name', '').strip()
        sd_str = request.form.get('start_date',  '').strip()
        ed_str = request.form.get('end_date',    '').strip()
        if not cname or not sd_str:
            flash('Name and start date are required.', 'danger')
            return render_template('add_client.html', coach_name=name,
                                   global_bl=global_bl, today=date.today().isoformat())
        client = Client(
            name=cname, coach_name=name,
            start_date=date.fromisoformat(sd_str),
            end_date=date.fromisoformat(ed_str) if ed_str else None,
            module_1_done='module_1' in request.form,
            module_2_done='module_2' in request.form,
            module_3_done='module_3' in request.form,
        )
        db.session.add(client)
        db.session.flush()
        for b in global_bl:
            raw = request.form.get(f'bl_{b.activity}', '').strip()
            db.session.add(ClientBaseline(
                client_id=client.id, activity=b.activity, label=b.label,
                value=int(raw) if raw.isdigit() else b.value))
        audit('add_client', f'Client: {cname}', f'Added to {name} roster, start {sd_str}')
        db.session.commit()
        flash(f'Client "{cname}" added.', 'success')
        return redirect(url_for('coach', name=name))
    return render_template('add_client.html', coach_name=name,
                           global_bl=global_bl, today=date.today().isoformat())


@app.route('/client/<int:cid>/edit', methods=['GET', 'POST'])
@login_required
def edit_client(cid):
    client    = Client.query.get_or_404(cid)
    if not can_write_client(client):
        flash('You can only edit your own clients.', 'danger')
        return redirect(url_for('coach', name=session['name']))
    global_bl = Baseline.query.order_by(Baseline.id).all()
    cb_map    = {b.activity: b.value for b in client.client_baselines}
    if request.method == 'POST':
        client.name = request.form.get('client_name', client.name).strip()
        sd = request.form.get('start_date', '').strip()
        ed = request.form.get('end_date',   '').strip()
        if sd: client.start_date = date.fromisoformat(sd)
        client.end_date     = date.fromisoformat(ed) if ed else None
        client.module_1_done = 'module_1' in request.form
        client.module_2_done = 'module_2' in request.form
        client.module_3_done = 'module_3' in request.form
        for b in global_bl:
            raw = request.form.get(f'bl_{b.activity}', '').strip()
            val = int(raw) if raw.isdigit() else b.value
            row = ClientBaseline.query.filter_by(
                client_id=client.id, activity=b.activity).first()
            if row: row.value = val
            else:   db.session.add(ClientBaseline(
                        client_id=client.id, activity=b.activity,
                        label=b.label, value=val))
        audit('edit_client', f'Client: {client.name}', 'Details updated')
        db.session.commit()
        flash('Client updated.', 'success')
        return redirect(url_for('client_weeks', cid=client.id))
    return render_template('edit_client.html', client=client,
                           global_bl=global_bl, cb_map=cb_map)


@app.route('/client/<int:cid>/archive', methods=['POST'])
@login_required
def archive_client(cid):
    client = Client.query.get_or_404(cid)
    if not can_write_client(client):
        flash('You can only archive your own clients.', 'danger')
        return redirect(url_for('coach', name=session['name']))
    name = client.coach_name
    client.active = False
    audit('archive_client', f'Client: {client.name}', 'Archived')
    db.session.commit()
    flash(f'"{client.name}" archived.', 'info')
    return redirect(url_for('coach', name=name))


@app.route('/client/<int:cid>/delete', methods=['POST'])
@login_required
def delete_client(cid):
    client = Client.query.get_or_404(cid)
    if not can_write_client(client):
        flash('You can only delete your own clients.', 'danger')
        return redirect(url_for('coach', name=session['name']))
    coach_name  = client.coach_name
    client_name = client.name
    # Explicitly delete WeeklyLog records first
    WeeklyLog.query.filter_by(client_id=cid).delete()
    audit('delete_client', f'Client: {client_name}',
          f'Permanently deleted from {coach_name}')
    db.session.delete(client)
    db.session.commit()
    flash(f'"{client_name}" permanently deleted.', 'danger')
    return redirect(url_for('coach', name=coach_name))


# ── Weekly logs ───────────────────────────────────────────────────────────────

@app.route('/client/<int:cid>/weeks')
@login_required
def client_weeks(cid):
    client = Client.query.get_or_404(cid)
    return render_template('client_weeks.html',
                           client=client,
                           weeks=client.weeks_progress(),
                           baselines=client.get_baselines(),
                           can_write=can_write_client(client))


@app.route('/client/<int:cid>/log')
@login_required
def log_current(cid):
    return redirect(url_for('log_week', cid=cid,
                            week_date=week_monday().isoformat()))


@app.route('/client/<int:cid>/log/<week_date>', methods=['GET', 'POST'])
@login_required
def log_week(cid, week_date):
    client = Client.query.get_or_404(cid)
    try:    ws = week_monday(date.fromisoformat(week_date))
    except: ws = week_monday()
    we        = week_friday(ws)
    log       = client.get_week_log(ws)
    baselines = client.get_baselines()

    if request.method == 'POST':
        if not can_write_client(client):
            flash('You can only log for your own clients.', 'danger')
            return redirect(url_for('client_weeks', cid=cid))
        comments     = int(request.form.get('comments')     or 0)
        posts        = int(request.form.get('posts')        or 0)
        outreach     = int(request.form.get('outreach')     or 0)
        applications = int(request.form.get('applications') or 0)
        follow_ups   = int(request.form.get('follow_ups')   or 0)
        notes        = request.form.get('notes', '')
        client.module_1_done = 'module_1' in request.form
        client.module_2_done = 'module_2' in request.form
        client.module_3_done = 'module_3' in request.form
        is_new = log is None
        if log:
            log.comments=comments; log.posts=posts; log.outreach=outreach
            log.applications=applications; log.follow_ups=follow_ups
            log.notes=notes; log.updated_at=datetime.utcnow()
        else:
            log = WeeklyLog(client_id=cid, week_start=ws, comments=comments,
                            posts=posts, outreach=outreach,
                            applications=applications, follow_ups=follow_ups,
                            notes=notes)
            db.session.add(log)
        if is_new:
            audit('log_week', f'Client: {client.name}',
                  f'Week {ws.strftime("%d %b")} logged – comments:{comments} posts:{posts} outreach:{outreach} apps:{applications}')
        else:
            audit('edit_log', f'Client: {client.name}',
                  f'Week {ws.strftime("%d %b")} updated')
        db.session.commit()
        low = [{'label': lbl, 'actual': actual, 'target': tgt, 'pct': actual/tgt*100}
               for _, lbl, actual, tgt in [
                   ('c','Comments',    comments,     baselines['comments']),
                   ('p','Posts',       posts,        baselines['posts']),
                   ('o','Outreach',    outreach,     baselines['outreach']),
                   ('a','Applications',applications, baselines['applications']),
                   ('f','Follow-ups',  follow_ups,   applications*2),
               ] if tgt > 0 and actual/tgt < 0.7]
        if low: send_alert(client.coach_name, client.name, ws, low)
        flash(f'Saved — {client.name}, week of {ws.strftime("%d %b")}.', 'success')
        return redirect(url_for('client_weeks', cid=cid))

    return render_template('log.html', client=client, log=log,
                           week_start=ws, week_end=we, baselines=baselines,
                           all_weeks=get_weeks_for_client(client),
                           can_write=can_write_client(client))


@app.route('/client/<int:cid>/log/<week_date>/delete', methods=['POST'])
@login_required
def delete_log(cid, week_date):
    client = Client.query.get_or_404(cid)
    if not can_write_client(client):
        flash('You can only delete logs for your own clients.', 'danger')
        return redirect(url_for('client_weeks', cid=cid))
    try:    ws = date.fromisoformat(week_date)
    except: return redirect(url_for('client_weeks', cid=cid))
    log = WeeklyLog.query.filter_by(client_id=cid, week_start=ws).first()
    if log:
        audit('delete_log', f'Client: {client.name}',
              f'Week {ws.strftime("%d %b")} deleted')
        db.session.delete(log)
        db.session.commit()
        flash(f'Log for week of {ws.strftime("%d %b")} deleted.', 'info')
    return redirect(url_for('client_weeks', cid=cid))


# ── Reports ───────────────────────────────────────────────────────────────────

def build_report_data(client, from_ws, to_ws):
    baselines = client.get_baselines()
    rows, wk  = [], 1
    for ws, we in get_weeks_for_client(client):
        if ws < from_ws: wk += 1; continue
        if ws > to_ws:   break
        log = client.get_week_log(ws)
        cpcts = log.compliance_pcts(baselines) if log else None
        rows.append({
            'wk': wk,
            'week_start': ws.isoformat(),
            'week_end':   we.isoformat(),
            'log': {'comments': log.comments, 'posts': log.posts,
                    'outreach': log.outreach, 'applications': log.applications,
                    'follow_ups': log.follow_ups, 'notes': log.notes,
                    'follow_up_target': log.follow_up_target()} if log else None,
            'pcts': cpcts,
            'overall': log.overall_pct(baselines) if log else None,
        })
        wk += 1
    logged = [r for r in rows if r['log']]
    avgs = None
    if logged:
        acts = ['comments','posts','outreach','applications','follow_ups']
        avgs = {a: round(sum(r['pcts'][a] for r in logged)/len(logged),1) for a in acts}
        avgs['overall'] = round(sum(r['overall'] for r in logged)/len(logged),1)
    return {
        'client_name': client.name, 'coach_name': client.coach_name,
        'from_week': from_ws.isoformat(), 'to_week': to_ws.isoformat(),
        'baselines': baselines,
        'module_1': client.module_1_done,
        'module_2': client.module_2_done,
        'module_3': client.module_3_done,
        'rows': rows, 'averages': avgs,
    }


def rows_with_dates(data_rows):
    out = []
    for r in data_rows:
        r2 = dict(r)
        r2['week_start'] = date.fromisoformat(r['week_start'])
        r2['week_end']   = date.fromisoformat(r['week_end'])
        out.append(r2)
    return out


@app.route('/client/<int:cid>/report')
@login_required
def client_report(cid):
    client    = Client.query.get_or_404(cid)
    all_weeks = get_weeks_for_client(client)
    from_ws   = all_weeks[0][0]  if all_weeks else week_monday()
    to_ws     = all_weeks[-1][0] if all_weeks else week_monday()
    try: from_ws = date.fromisoformat(request.args['from_week'])
    except: pass
    try: to_ws   = date.fromisoformat(request.args['to_week'])
    except: pass
    data    = build_report_data(client, from_ws, to_ws)
    summary = generate_summary(client.name, data['averages'])
    return render_template('report.html',
                           client=client,
                           baselines=data['baselines'],
                           rows=rows_with_dates(data['rows']),
                           all_weeks=all_weeks,
                           from_ws=from_ws, to_ws=to_ws,
                           averages=data['averages'],
                           summary=summary,
                           report_json=json.dumps(data),
                           generated=datetime.now(),
                           can_write=can_write_client(client))


@app.route('/client/<int:cid>/report/save', methods=['POST'])
@login_required
def save_report(cid):
    client = Client.query.get_or_404(cid)
    try:
        from_ws = date.fromisoformat(request.form['from_week'])
        to_ws   = date.fromisoformat(request.form['to_week'])
    except (KeyError, ValueError):
        flash('Invalid date range.', 'danger')
        return redirect(url_for('client_report', cid=cid))
    data = build_report_data(client, from_ws, to_ws)
    db.session.add(ReportSent(
        client_id=cid, sent_by=session['name'],
        from_week=from_ws, to_week=to_ws,
        report_json=json.dumps(data)))
    audit('mark_sent', f'Client: {client.name}',
          f'Report {from_ws}–{to_ws} marked sent')
    db.session.commit()
    flash(f'Report snapshot saved — marked as sent by {session["name"]}.', 'success')
    return redirect(url_for('client_report', cid=cid,
                            from_week=from_ws.isoformat(), to_week=to_ws.isoformat()))


@app.route('/client/<int:cid>/report/history')
@login_required
def report_history(cid):
    client  = Client.query.get_or_404(cid)
    reports = (ReportSent.query.filter_by(client_id=cid)
               .order_by(ReportSent.sent_at.desc()).all())
    return render_template('report_history.html', client=client, reports=reports)


@app.route('/client/<int:cid>/report/history/<int:rid>')
@login_required
def view_sent_report(cid, rid):
    client = Client.query.get_or_404(cid)
    rs     = ReportSent.query.get_or_404(rid)
    data   = json.loads(rs.report_json)
    return render_template('report_sent_view.html',
                           client=client, rs=rs, data=data,
                           rows=rows_with_dates(data['rows']),
                           baselines=data['baselines'])


# ── Audit Log ─────────────────────────────────────────────────────────────────

@app.route('/audit-log')
def audit_log_view():
    # Purge entries older than 30 days
    cutoff = datetime.utcnow() - timedelta(days=30)
    AuditLog.query.filter(AuditLog.timestamp < cutoff).delete()
    db.session.commit()

    coach_filter = request.args.get('coach', '')
    q = AuditLog.query
    if coach_filter:
        q = q.filter(AuditLog.actor == coach_filter)
    entries = q.order_by(AuditLog.timestamp.desc()).limit(200).all()

    all_actors = [r[0] for r in db.session.query(AuditLog.actor).distinct().all()]

    return render_template('audit_log.html',
                           entries=entries,
                           all_actors=all_actors,
                           coach_filter=coach_filter,
                           coaches=COACHES + FOUNDERS)


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    offset = int(request.args.get('w', 0))
    ws     = week_monday() + timedelta(weeks=offset)
    we     = week_friday(ws)

    coach_rows = []
    for cname in COACHES:
        clients = Client.query.filter_by(coach_name=cname, active=True).all()
        total   = len(clients)
        m1 = sum(1 for c in clients if c.module_1_done)
        m2 = sum(1 for c in clients if c.module_2_done)
        m3 = sum(1 for c in clients if c.module_3_done)
        acts   = ['comments','posts','outreach','applications','follow_ups']
        actual = {a: 0 for a in acts}
        target = {a: 0 for a in acts}
        logged = 0
        client_rows = []
        for c in clients:
            bl  = c.get_baselines()
            log = c.get_week_log(ws)
            if log:
                logged += 1
                actual['comments']     += log.comments
                actual['posts']        += log.posts
                actual['outreach']     += log.outreach
                actual['applications'] += log.applications
                actual['follow_ups']   += log.follow_ups
                target['follow_ups']   += log.follow_up_target()
            for a in ['comments','posts','outreach','applications']:
                target[a] += bl[a]
            client_rows.append({'client': c, 'log': log,
                                'statuses': log.statuses(bl) if log else None,
                                'bl': bl})
        _p  = lambda a: pct(actual[a], target[a]) if target[a] else 100.0
        _mp = lambda n: round(n / total * 100) if total else 0
        act_pcts = {a: _p(a) for a in acts}
        coach_rows.append({
            'name': cname, 'total': total, 'logged': logged,
            'client_rows': client_rows,
            'm1': _mp(m1), 'm2': _mp(m2), 'm3': _mp(m3),
            'acts': act_pcts,
            'overall': round(sum(act_pcts[k] * w for k, w in WEIGHTS.items()), 1),
        })

    # Coach activity — visible to everyone (creates healthy competition)
    this_monday  = week_monday()
    week_start_dt = datetime(this_monday.year, this_monday.month, this_monday.day)
    activity_rows = []
    for cname in COACHES + FOUNDERS:
        role = 'founder' if cname in FOUNDERS else 'coach'
        week_logins = CoachSession.query.filter(
            CoachSession.name == cname,
            CoachSession.logged_in_at >= week_start_dt).count()
        last = (CoachSession.query.filter_by(name=cname)
                .order_by(CoachSession.logged_in_at.desc()).first())
        if role == 'coach':
            cl = Client.query.filter_by(coach_name=cname, active=True).all()
            tc = len(cl)
            lc = sum(1 for c in cl if c.get_week_log(this_monday))
        else:
            tc = lc = None
        activity_rows.append({
            'name': cname, 'role': role,
            'week_logins': week_logins,
            'last_login':  last.logged_in_at if last else None,
            'total_clients': tc, 'logged_clients': lc,
        })

    return render_template('dashboard.html',
                           coach_rows=coach_rows,
                           week_start=ws, week_end=we, offset=offset,
                           activity_rows=activity_rows)


# ── Settings ──────────────────────────────────────────────────────────────────

@app.route('/settings', methods=['GET'])
@founder_required
def settings():
    return render_template('settings.html',
                           baselines=Baseline.query.order_by(Baseline.id).all())


@app.route('/settings', methods=['POST'])
@founder_required
def settings_save():
    for b in Baseline.query.all():
        raw = request.form.get(b.activity, '').strip()
        if raw.isdigit(): b.value = int(raw)
    db.session.commit()
    flash('Global baselines updated.', 'success')
    return redirect(url_for('settings'))


# ── Seed ──────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    if Baseline.query.count() == 0:
        for b in DEFAULT_BASELINES:
            db.session.add(Baseline(**b))
        db.session.commit()

if __name__ == '__main__':
    app.run(debug=True)
