from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import date, datetime, timedelta
import smtplib
from email.mime.text import MIMEText
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'tjw-dev-secret')

database_url = os.environ.get('DATABASE_URL', 'sqlite:///tracker.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

COACHES = ['Shruti', 'Veena', 'Poushali', 'Anindya', 'Swati', 'Ajay']
ALERT_EMAIL = 'athejobworkshop@gmail.com'

DEFAULT_BASELINES = [
    {'activity': 'comments',     'label': 'Comments',     'value': 25, 'suggested': 25},
    {'activity': 'posts',        'label': 'Posts',        'value': 3,  'suggested': 3},
    {'activity': 'outreach',     'label': 'Outreach',     'value': 50, 'suggested': 50},
    {'activity': 'applications', 'label': 'Applications', 'value': 18, 'suggested': 18},
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def week_monday(d=None):
    if d is None:
        d = date.today()
    return d - timedelta(days=d.weekday())

def week_friday(monday):
    return monday + timedelta(days=4)

def get_weeks_for_client(client):
    """All Mon–Fri weeks from client's start_date to this week, oldest first."""
    weeks = []
    start = week_monday(client.start_date)
    this_monday = week_monday()
    current = start
    while current <= this_monday:
        weeks.append((current, week_friday(current)))
        current += timedelta(weeks=1)
    return weeks

def activity_status(actual, target):
    if target == 0:
        return 'success'
    if actual >= target:
        return 'success'
    if actual >= target * 0.7:
        return 'warning'
    return 'danger'

def pct(actual, target):
    if target == 0:
        return 100.0
    return min(100.0, round(actual / target * 100, 1))


# ── Models ────────────────────────────────────────────────────────────────────

class Baseline(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    activity  = db.Column(db.String(50), unique=True, nullable=False)
    label     = db.Column(db.String(100), nullable=False)
    value     = db.Column(db.Integer, nullable=False)
    suggested = db.Column(db.Integer, nullable=False)


class Client(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(100), nullable=False)
    coach_name   = db.Column(db.String(50), nullable=False)
    start_date   = db.Column(db.Date, nullable=False, default=date.today)
    end_date     = db.Column(db.Date, nullable=True)
    active       = db.Column(db.Boolean, default=True)
    module_1_done = db.Column(db.Boolean, default=False)
    module_2_done = db.Column(db.Boolean, default=False)
    module_3_done = db.Column(db.Boolean, default=False)
    logs         = db.relationship('WeeklyLog', backref='client', lazy=True,
                                   order_by='WeeklyLog.week_start.desc()')
    client_baselines = db.relationship('ClientBaseline', backref='client',
                                       lazy=True, cascade='all, delete-orphan')

    def get_baselines(self):
        if self.client_baselines:
            return {b.activity: b.value for b in self.client_baselines}
        return {b.activity: b.value
                for b in Baseline.query.order_by(Baseline.id).all()}

    def get_week_log(self, monday):
        return WeeklyLog.query.filter_by(
            client_id=self.id, week_start=monday).first()

    def current_week_log(self):
        return self.get_week_log(week_monday())

    def weeks_progress(self):
        """Returns list of dicts for each week since start_date."""
        baselines = self.get_baselines()
        result = []
        for ws, we in get_weeks_for_client(self):
            log = self.get_week_log(ws)
            result.append({
                'week_start': ws,
                'week_end':   we,
                'log':        log,
                'compliance': log.overall_pct(baselines) if log else None,
                'statuses':   log.statuses(baselines)    if log else None,
            })
        result.reverse()  # most recent first
        return result


class ClientBaseline(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    client_id  = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False)
    activity   = db.Column(db.String(50), nullable=False)
    label      = db.Column(db.String(100), nullable=False)
    value      = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint('client_id', 'activity',
                                          name='uq_cb_client_activity'),)


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
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow,
                             onupdate=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('client_id', 'week_start',
                                          name='uq_wl_client_week'),)

    def week_end(self):
        return self.week_start + timedelta(days=4)

    def follow_up_target(self):
        return self.applications * 2

    def compliance_pcts(self, baselines):
        fut = self.follow_up_target()
        return {
            'comments':     pct(self.comments,     baselines['comments']),
            'posts':        pct(self.posts,         baselines['posts']),
            'outreach':     pct(self.outreach,      baselines['outreach']),
            'applications': pct(self.applications,  baselines['applications']),
            'follow_ups':   pct(self.follow_ups, fut) if fut > 0 else 100.0,
        }

    def overall_pct(self, baselines):
        vals = list(self.compliance_pcts(baselines).values())
        return round(sum(vals) / len(vals), 1)

    def statuses(self, baselines):
        return {
            'comments':     activity_status(self.comments,    baselines['comments']),
            'posts':        activity_status(self.posts,        baselines['posts']),
            'outreach':     activity_status(self.outreach,     baselines['outreach']),
            'applications': activity_status(self.applications, baselines['applications']),
            'follow_ups':   activity_status(self.follow_ups,   self.follow_up_target()),
        }


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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    h = datetime.now().hour
    greeting = 'morning' if h < 12 else 'afternoon' if h < 17 else 'evening'
    return render_template('index.html', coaches=COACHES, greeting=greeting)


# ── Coach ─────────────────────────────────────────────────────────────────────

@app.route('/coach/<name>')
def coach(name):
    if name not in COACHES:
        return redirect(url_for('index'))
    clients = (Client.query
               .filter_by(coach_name=name, active=True)
               .order_by(Client.name).all())
    this_week = week_monday()
    client_cards = []
    for c in clients:
        weeks = get_weeks_for_client(c)
        total_weeks = len(weeks)
        logged_weeks = sum(1 for ws, _ in weeks if c.get_week_log(ws))
        log = c.current_week_log()
        client_cards.append({
            'client':       c,
            'total_weeks':  total_weeks,
            'logged_weeks': logged_weeks,
            'this_log':     log,
            'baselines':    c.get_baselines(),
        })
    return render_template('coach.html',
                           coach_name=name,
                           client_cards=client_cards,
                           this_week=this_week,
                           this_week_end=week_friday(this_week))


@app.route('/coach/<name>/add_client', methods=['GET', 'POST'])
def add_client(name):
    if name not in COACHES:
        return redirect(url_for('index'))
    global_bl = Baseline.query.order_by(Baseline.id).all()

    if request.method == 'POST':
        cname   = request.form.get('client_name', '').strip()
        sd_str  = request.form.get('start_date', '').strip()
        ed_str  = request.form.get('end_date',   '').strip()
        if not cname or not sd_str:
            flash('Client name and start date are required.', 'danger')
            return render_template('add_client.html', coach_name=name,
                                   global_bl=global_bl, today=date.today().isoformat())
        client = Client(
            name         = cname,
            coach_name   = name,
            start_date   = date.fromisoformat(sd_str),
            end_date     = date.fromisoformat(ed_str) if ed_str else None,
            module_1_done = 'module_1' in request.form,
            module_2_done = 'module_2' in request.form,
            module_3_done = 'module_3' in request.form,
        )
        db.session.add(client)
        db.session.flush()
        for b in global_bl:
            raw = request.form.get(f'bl_{b.activity}', '').strip()
            val = int(raw) if raw.isdigit() else b.value
            db.session.add(ClientBaseline(
                client_id=client.id, activity=b.activity,
                label=b.label, value=val))
        db.session.commit()
        flash(f'Client "{cname}" added.', 'success')
        return redirect(url_for('coach', name=name))

    return render_template('add_client.html', coach_name=name,
                           global_bl=global_bl, today=date.today().isoformat())


# ── Client management ─────────────────────────────────────────────────────────

@app.route('/client/<int:cid>/edit', methods=['GET', 'POST'])
def edit_client(cid):
    client  = Client.query.get_or_404(cid)
    global_bl = Baseline.query.order_by(Baseline.id).all()
    cb_map  = {b.activity: b.value for b in client.client_baselines}

    if request.method == 'POST':
        client.name   = request.form.get('client_name', client.name).strip()
        sd_str = request.form.get('start_date', '').strip()
        ed_str = request.form.get('end_date',   '').strip()
        if sd_str:
            client.start_date = date.fromisoformat(sd_str)
        client.end_date     = date.fromisoformat(ed_str) if ed_str else None
        client.module_1_done = 'module_1' in request.form
        client.module_2_done = 'module_2' in request.form
        client.module_3_done = 'module_3' in request.form
        for b in global_bl:
            raw = request.form.get(f'bl_{b.activity}', '').strip()
            val = int(raw) if raw.isdigit() else b.value
            row = ClientBaseline.query.filter_by(
                client_id=client.id, activity=b.activity).first()
            if row:
                row.value = val
            else:
                db.session.add(ClientBaseline(
                    client_id=client.id, activity=b.activity,
                    label=b.label, value=val))
        db.session.commit()
        flash('Client updated.', 'success')
        return redirect(url_for('client_weeks', cid=client.id))

    return render_template('edit_client.html', client=client,
                           global_bl=global_bl, cb_map=cb_map)


@app.route('/client/<int:cid>/archive', methods=['POST'])
def archive_client(cid):
    client = Client.query.get_or_404(cid)
    name   = client.coach_name
    client.active = False
    db.session.commit()
    flash(f'"{client.name}" archived.', 'info')
    return redirect(url_for('coach', name=name))


# ── Logging ───────────────────────────────────────────────────────────────────

@app.route('/client/<int:cid>/weeks')
def client_weeks(cid):
    client = Client.query.get_or_404(cid)
    return render_template('client_weeks.html',
                           client=client,
                           weeks=client.weeks_progress(),
                           baselines=client.get_baselines())


@app.route('/client/<int:cid>/log')
def log_current(cid):
    return redirect(url_for('log_week', cid=cid,
                            week_date=week_monday().isoformat()))


@app.route('/client/<int:cid>/log/<week_date>', methods=['GET', 'POST'])
def log_week(cid, week_date):
    client    = Client.query.get_or_404(cid)
    try:
        ws = date.fromisoformat(week_date)
    except ValueError:
        ws = week_monday()
    ws        = week_monday(ws)   # ensure it's always a Monday
    we        = week_friday(ws)
    log       = client.get_week_log(ws)
    baselines = client.get_baselines()

    if request.method == 'POST':
        comments     = int(request.form.get('comments')     or 0)
        posts        = int(request.form.get('posts')        or 0)
        outreach     = int(request.form.get('outreach')     or 0)
        applications = int(request.form.get('applications') or 0)
        follow_ups   = int(request.form.get('follow_ups')   or 0)
        notes        = request.form.get('notes', '')

        client.module_1_done = 'module_1' in request.form
        client.module_2_done = 'module_2' in request.form
        client.module_3_done = 'module_3' in request.form

        if log:
            log.comments     = comments
            log.posts        = posts
            log.outreach     = outreach
            log.applications = applications
            log.follow_ups   = follow_ups
            log.notes        = notes
            log.updated_at   = datetime.utcnow()
        else:
            log = WeeklyLog(client_id=cid, week_start=ws,
                            comments=comments, posts=posts,
                            outreach=outreach, applications=applications,
                            follow_ups=follow_ups, notes=notes)
            db.session.add(log)
        db.session.commit()

        # Email alert for anything below 70 %
        fut  = applications * 2
        low  = []
        for act, label, actual, target in [
            ('comments',     'Comments',     comments,     baselines['comments']),
            ('posts',        'Posts',        posts,        baselines['posts']),
            ('outreach',     'Outreach',     outreach,     baselines['outreach']),
            ('applications', 'Applications', applications, baselines['applications']),
            ('follow_ups',   'Follow-ups',   follow_ups,   fut),
        ]:
            if target > 0 and actual / target < 0.7:
                low.append({'label': label, 'actual': actual,
                            'target': target, 'pct': actual / target * 100})
        if low:
            send_alert(client.coach_name, client.name, ws, low)

        flash(f'Saved — {client.name}, week of {ws.strftime("%d %b")}.', 'success')
        return redirect(url_for('client_weeks', cid=cid))

    # Build list of all week options for the "jump to week" selector
    all_weeks = get_weeks_for_client(client)
    return render_template('log.html', client=client, log=log,
                           week_start=ws, week_end=we,
                           baselines=baselines, all_weeks=all_weeks)


# ── Report ────────────────────────────────────────────────────────────────────

@app.route('/client/<int:cid>/report')
def client_report(cid):
    client    = Client.query.get_or_404(cid)
    baselines = client.get_baselines()
    all_weeks = get_weeks_for_client(client)

    # Default: full range
    from_ws = all_weeks[0][0]  if all_weeks else week_monday()
    to_ws   = all_weeks[-1][0] if all_weeks else week_monday()

    if request.args.get('from_week'):
        try:
            from_ws = date.fromisoformat(request.args['from_week'])
        except ValueError:
            pass
    if request.args.get('to_week'):
        try:
            to_ws = date.fromisoformat(request.args['to_week'])
        except ValueError:
            pass

    rows = []
    wk_num = 1
    for ws, we in all_weeks:
        if ws < from_ws:
            wk_num += 1
            continue
        if ws > to_ws:
            break
        log = client.get_week_log(ws)
        cpcts = log.compliance_pcts(baselines) if log else None
        rows.append({
            'wk':         wk_num,
            'week_start': ws,
            'week_end':   we,
            'log':        log,
            'pcts':       cpcts,
            'overall':    log.overall_pct(baselines) if log else None,
        })
        wk_num += 1

    # Averages across logged rows
    logged = [r for r in rows if r['log']]
    averages = None
    if logged:
        acts = ['comments', 'posts', 'outreach', 'applications', 'follow_ups']
        averages = {a: round(sum(r['pcts'][a] for r in logged) / len(logged), 1)
                    for a in acts}
        averages['overall'] = round(sum(r['overall'] for r in logged) / len(logged), 1)

    return render_template('report.html',
                           client=client, baselines=baselines,
                           rows=rows, all_weeks=all_weeks,
                           from_ws=from_ws, to_ws=to_ws,
                           averages=averages,
                           generated=datetime.now())


# ── Dashboard (founder) ───────────────────────────────────────────────────────

@app.route('/dashboard')
def dashboard():
    offset = int(request.args.get('w', 0))
    ws     = week_monday() + timedelta(weeks=offset)
    we     = week_friday(ws)

    coach_rows = []
    for cname in COACHES:
        clients = (Client.query
                   .filter_by(coach_name=cname, active=True).all())
        total   = len(clients)

        m1 = sum(1 for c in clients if c.module_1_done)
        m2 = sum(1 for c in clients if c.module_2_done)
        m3 = sum(1 for c in clients if c.module_3_done)

        acts   = ['comments', 'posts', 'outreach', 'applications', 'follow_ups']
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
            for a in ['comments', 'posts', 'outreach', 'applications']:
                target[a] += bl[a]
            client_rows.append({
                'client':   c,
                'log':      log,
                'statuses': log.statuses(bl) if log else None,
                'bl':       bl,
            })

        def _pct(a):
            return pct(actual[a], target[a]) if target[a] else 100.0

        act_pcts    = {a: _pct(a) for a in acts}
        overall_pct = round(sum(act_pcts.values()) / len(acts), 1)

        def _mpct(n):
            return round(n / total * 100) if total else 0

        coach_rows.append({
            'name':        cname,
            'total':       total,
            'logged':      logged,
            'client_rows': client_rows,
            'm1':          _mpct(m1),
            'm2':          _mpct(m2),
            'm3':          _mpct(m3),
            'acts':        act_pcts,
            'overall':     overall_pct,
        })

    return render_template('dashboard.html',
                           coach_rows=coach_rows,
                           week_start=ws, week_end=we,
                           offset=offset)


# ── Settings ──────────────────────────────────────────────────────────────────

@app.route('/settings', methods=['GET'])
def settings():
    return render_template('settings.html',
                           baselines=Baseline.query.order_by(Baseline.id).all())

@app.route('/settings', methods=['POST'])
def settings_save():
    for b in Baseline.query.all():
        raw = request.form.get(b.activity, '').strip()
        if raw.isdigit():
            b.value = int(raw)
    db.session.commit()
    flash('Global baselines updated.', 'success')
    return redirect(url_for('settings'))


# ── Boot ──────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    if Baseline.query.count() == 0:
        for b in DEFAULT_BASELINES:
            db.session.add(Baseline(**b))
        db.session.commit()

if __name__ == '__main__':
    app.run(debug=True)
