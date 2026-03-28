from flask import Flask, request, render_template, redirect, url_for, session, flash
import mysql.connector
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = "smart_parking_secret_key"


def get_db_connection():
    return mysql.connector.connect(
        host="127.0.0.1",
        user="parking_user",
        password="1234",
        database="smart_parking",
        auth_plugin="mysql_native_password"
    )


def has_capacity_for_reservation(cursor, zone_id, start_dt, end_dt):
    cursor.execute("""
        SELECT total_spots
        FROM parking_zones
        WHERE id = %s AND status = 'active'
        LIMIT 1
    """, (zone_id,))
    zone = cursor.fetchone()

    if not zone:
        return False

    total_spots = int(zone["total_spots"])

    cursor.execute("""
        SELECT COUNT(*) AS overlapping_count
        FROM parking_reservations
        WHERE zone_id = %s
          AND status = 'active'
          AND (%s < reservation_end AND %s > reservation_start)
    """, (zone_id, start_dt, end_dt))

    overlapping_count = int(cursor.fetchone()["overlapping_count"])

    return overlapping_count < total_spots


def expire_unused_reservations():
    conn = None
    cursor = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        grace_minutes = 15
        cutoff_time = datetime.now() - timedelta(minutes=grace_minutes)

        cursor.execute("""
            SELECT 
                pr.id,
                pr.zone_id,
                pr.reservation_start,
                pr.reservation_end,
                pz.price_per_hour
            FROM parking_reservations pr
            JOIN parking_zones pz ON pr.zone_id = pz.id
            LEFT JOIN parking_sessions ps
                ON pr.id = ps.reservation_id
                AND ps.status = 'active'
            WHERE pr.status = 'active'
              AND pr.reservation_start <= %s
              AND ps.id IS NULL
        """, (cutoff_time,))

        expired_reservations = cursor.fetchall()

        for reservation in expired_reservations:
            duration_hours = (
                reservation["reservation_end"] - reservation["reservation_start"]
            ).total_seconds() / 3600

            if duration_hours < 0:
                duration_hours = 0

            penalty_fee = round(duration_hours * float(reservation["price_per_hour"]), 2)

            cursor.execute("""
                UPDATE parking_reservations
                SET status = 'expired',
                    penalty_fee = %s,
                    notes = %s
                WHERE id = %s
            """, (
                penalty_fee,
                "No-show: rezervarea a expirat fără intrare în parcare.",
                reservation["id"]
            ))

        conn.commit()

    except mysql.connector.Error as err:
        print(f"Eroare la expirarea rezervărilor: {err}")

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@app.route("/")
def home():
    if "user_id" not in session:
        return redirect(url_for("login"))

    expire_unused_reservations()

    conn = None
    cursor = None
    parking_zones = []

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT id, zone_name, location_description, total_spots, available_spots, price_per_hour, status
            FROM parking_zones
            WHERE status = 'active'
            ORDER BY zone_name ASC
        """)
        parking_zones = cursor.fetchall()

    except mysql.connector.Error as err:
        print(f"Eroare MySQL: {err}")

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return render_template(
        "home.html",
        full_name=session.get("full_name"),
        role=session.get("role"),
        parking_zones=parking_zones
    )


@app.route("/admin")
def admin_dashboard():
    if "user_id" not in session or session.get("role") != "admin":
        return redirect(url_for("home"))

    expire_unused_reservations()

    conn = None
    cursor = None
    stats = {}
    reservations = []
    sessions = []

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        reservation_status = request.args.get("reservation_status", "all").strip().lower()
        session_status = request.args.get("session_status", "all").strip().lower()

        allowed_reservation_statuses = {"all", "active", "expired", "cancelled", "completed"}
        allowed_session_statuses = {"all", "active", "finished"}

        if reservation_status not in allowed_reservation_statuses:
            reservation_status = "all"

        if session_status not in allowed_session_statuses:
            session_status = "all"

        cursor.execute("SELECT COUNT(*) AS total FROM parking_reservations")
        stats["total_reservations"] = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) AS total FROM parking_reservations WHERE status = 'active'")
        stats["active_reservations"] = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) AS total FROM parking_reservations WHERE status = 'expired'")
        stats["expired_reservations"] = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) AS total FROM parking_reservations WHERE status = 'cancelled'")
        stats["cancelled_reservations"] = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) AS total FROM parking_reservations WHERE status = 'completed'")
        stats["completed_reservations"] = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) AS total FROM parking_sessions")
        stats["total_sessions"] = cursor.fetchone()["total"]

        cursor.execute("SELECT IFNULL(SUM(total_cost), 0) AS total FROM parking_sessions")
        stats["sessions_revenue"] = float(cursor.fetchone()["total"])

        cursor.execute("SELECT IFNULL(SUM(penalty_fee), 0) AS total FROM parking_reservations")
        stats["penalties_revenue"] = float(cursor.fetchone()["total"])

        stats["total_revenue"] = round(
            stats["sessions_revenue"] + stats["penalties_revenue"], 2
        )

        cursor.execute("""
            SELECT SUM(total_spots - available_spots) AS occupied
            FROM parking_zones
        """)
        occupied = cursor.fetchone()["occupied"]
        stats["occupied_spots"] = occupied if occupied is not None else 0

        reservations_query = """
            SELECT 
                pr.id,
                pr.license_plate,
                pr.reservation_start,
                pr.reservation_end,
                pr.status,
                pr.penalty_fee,
                pr.notes,
                pr.created_at,
                pz.zone_name
            FROM parking_reservations pr
            JOIN parking_zones pz ON pr.zone_id = pz.id
            WHERE 1=1
        """
        reservations_params = []

        if reservation_status != "all":
            reservations_query += " AND pr.status = %s"
            reservations_params.append(reservation_status)

        reservations_query += " ORDER BY pr.created_at DESC LIMIT 10"

        cursor.execute(reservations_query, tuple(reservations_params))
        reservations = cursor.fetchall()

        sessions_query = """
            SELECT 
                ps.id,
                ps.license_plate,
                ps.start_time,
                ps.end_time,
                ps.total_cost,
                ps.status,
                pz.zone_name
            FROM parking_sessions ps
            JOIN parking_zones pz ON ps.zone_id = pz.id
            WHERE 1=1
        """
        sessions_params = []

        if session_status != "all":
            sessions_query += " AND ps.status = %s"
            sessions_params.append(session_status)

        sessions_query += " ORDER BY ps.start_time DESC LIMIT 10"

        cursor.execute(sessions_query, tuple(sessions_params))
        sessions = cursor.fetchall()

    except mysql.connector.Error as err:
        flash(f"Eroare MySQL: {err}", "error")
        return redirect(url_for("home"))

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return render_template(
        "admin_dashboard.html",
        stats=stats,
        reservations=reservations,
        sessions=sessions,
        reservation_status=reservation_status,
        session_status=session_status
    )

@app.route("/reserve/<int:zone_id>", methods=["GET", "POST"])
def reserve(zone_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    expire_unused_reservations()

    conn = None
    cursor = None
    zone = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT id, zone_name, location_description, total_spots, available_spots, price_per_hour, status
            FROM parking_zones
            WHERE id = %s AND status = 'active'
        """, (zone_id,))
        zone = cursor.fetchone()

        if not zone:
            flash("Zona selectată nu este disponibilă.", "error")
            return redirect(url_for("home"))

        if request.method == "POST":
            license_plate = request.form.get("license_plate", "").strip().upper()
            reservation_start = request.form.get("reservation_start", "").strip()
            reservation_end = request.form.get("reservation_end", "").strip()

            if not license_plate or not reservation_start or not reservation_end:
                flash("Completează toate câmpurile.", "error")
                return redirect(url_for("reserve", zone_id=zone_id))

            try:
                start_dt = datetime.strptime(reservation_start, "%Y-%m-%dT%H:%M")
                end_dt = datetime.strptime(reservation_end, "%Y-%m-%dT%H:%M")
                now = datetime.now()

                if start_dt < now:
                    flash("Nu poți face o rezervare în trecut.", "error")
                    return redirect(url_for("reserve", zone_id=zone_id))

                if end_dt <= start_dt:
                    flash("Data de final trebuie să fie după data de început.", "error")
                    return redirect(url_for("reserve", zone_id=zone_id))

                cursor.execute("""
                    SELECT id
                    FROM parking_reservations
                    WHERE license_plate = %s
                      AND status = 'active'
                      AND (%s < reservation_end AND %s > reservation_start)
                    LIMIT 1
                """, (license_plate, start_dt, end_dt))
                overlapping_reservation = cursor.fetchone()

                if overlapping_reservation:
                    flash("Există deja o rezervare activă suprapusă pentru acest număr de înmatriculare.", "error")
                    return redirect(url_for("reserve", zone_id=zone_id))

                cursor.execute("""
                    SELECT id
                    FROM parking_sessions
                    WHERE license_plate = %s
                      AND status = 'active'
                    LIMIT 1
                """, (license_plate,))
                active_session = cursor.fetchone()

                if active_session:
                    flash("Există deja o sesiune activă pentru acest număr de înmatriculare.", "error")
                    return redirect(url_for("reserve", zone_id=zone_id))

                if not has_capacity_for_reservation(cursor, zone_id, start_dt, end_dt):
                    flash("Nu mai există capacitate disponibilă în intervalul selectat pentru această zonă.", "error")
                    return redirect(url_for("reserve", zone_id=zone_id))

                cursor.execute("""
                    INSERT INTO parking_reservations
                    (user_id, zone_id, license_plate, reservation_start, reservation_end, status)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    session["user_id"],
                    zone_id,
                    license_plate,
                    start_dt,
                    end_dt,
                    "active"
                ))

                conn.commit()

                flash("Rezervarea a fost creată cu succes!", "success")
                return redirect(url_for("my_reservations"))

            except ValueError:
                flash("Formatul datei nu este valid.", "error")
                return redirect(url_for("reserve", zone_id=zone_id))

    except mysql.connector.Error as err:
        flash(f"Eroare MySQL: {err}", "error")
        return redirect(url_for("home"))

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return render_template("reserve.html", zone=zone)

@app.route("/my-reservations")
def my_reservations():
    if "user_id" not in session:
        return redirect(url_for("login"))

    expire_unused_reservations()

    conn = None
    cursor = None
    reservations = []

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        selected_status = request.args.get("status", "all")
        print("STATUS DIN URL:", selected_status)

        query = """
            SELECT 
                pr.id,
                pz.zone_name,
                pr.license_plate,
                pr.reservation_start,
                pr.reservation_end,
                pr.status,
                pr.penalty_fee,
                pr.notes,
                pr.created_at,
                ps.total_cost
            FROM parking_reservations pr
            JOIN parking_zones pz ON pr.zone_id = pz.id
            LEFT JOIN parking_sessions ps ON pr.id = ps.reservation_id
            WHERE pr.user_id = %s
        """

        params = [session["user_id"]]

        if selected_status != "all":
            query += " AND pr.status = %s"
            params.append(selected_status)

        query += " ORDER BY pr.created_at DESC"

        print("QUERY FINAL:", query)
        print("PARAMS:", params)

        cursor.execute(query, tuple(params))
        reservations = cursor.fetchall()

    except mysql.connector.Error as err:
        print(f"Eroare MySQL: {err}")

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return render_template(
        "my_reservations.html",
        reservations=reservations,
        selected_status=selected_status
    )

@app.route("/cancel-reservation/<int:reservation_id>", methods=["POST"])
def cancel_reservation(reservation_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    expire_unused_reservations()

    conn = None
    cursor = None
    cancelled = False

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT pr.*, pz.price_per_hour
            FROM parking_reservations pr
            JOIN parking_zones pz ON pr.zone_id = pz.id
            WHERE pr.id = %s AND pr.user_id = %s
        """, (reservation_id, session["user_id"]))
        reservation = cursor.fetchone()

        if not reservation:
            flash("Rezervarea nu a fost găsită.", "error")
            return redirect(url_for("my_reservations"))

        if reservation["status"] != "active":
            flash("Doar rezervările active pot fi anulate.", "warning")
            return redirect(url_for("my_reservations"))

        cursor.execute("""
            SELECT id
            FROM parking_sessions
            WHERE reservation_id = %s
              AND status = 'active'
            LIMIT 1
        """, (reservation_id,))
        active_session = cursor.fetchone()

        if active_session:
            flash("Rezervarea nu mai poate fi anulată deoarece există deja o sesiune activă.", "error")
            return redirect(url_for("my_reservations"))

        now = datetime.now()
        penalty_fee = 0.00
        notes = "Rezervare anulată la timp."

        duration_hours = (
            reservation["reservation_end"] - reservation["reservation_start"]
        ).total_seconds() / 3600

        if duration_hours < 0:
            duration_hours = 0

        full_reservation_cost = duration_hours * float(reservation["price_per_hour"])

        if now >= reservation["reservation_start"]:
            penalty_fee = round(full_reservation_cost * 0.5, 2)
            notes = "Late cancellation: anulare după începerea intervalului rezervat."

        cursor.execute("""
            UPDATE parking_reservations
            SET status = 'cancelled',
                penalty_fee = %s,
                notes = %s
            WHERE id = %s
        """, (penalty_fee, notes, reservation_id))

        conn.commit()
        cancelled = True

    except mysql.connector.Error as err:
        flash(f"Eroare MySQL: {err}", "error")

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    if cancelled:
        flash("Rezervarea a fost anulată cu succes.", "success")

    return redirect(url_for("my_reservations"))



@app.route("/barrier-access", methods=["GET", "POST"])
def barrier_access():
    if "user_id" not in session:
        return redirect(url_for("login"))

    expire_unused_reservations()

    conn = None
    cursor = None
    parking_zones = []

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT id, zone_name, available_spots, status
            FROM parking_zones
            WHERE status = 'active'
            ORDER BY zone_name ASC
        """)
        parking_zones = cursor.fetchall()

        if request.method == "POST":
            license_plate = request.form.get("license_plate", "").strip().upper()
            selected_zone_id = request.form.get("zone_id", "").strip()
            now = datetime.now()

            if not license_plate:
                flash("Introdu numărul de înmatriculare.", "error")
                return redirect(url_for("barrier_access"))

            cursor.execute("""
                SELECT id
                FROM parking_sessions
                WHERE license_plate = %s
                  AND status = 'active'
                LIMIT 1
            """, (license_plate,))
            existing_active_session = cursor.fetchone()

            if existing_active_session:
                flash("Există deja o sesiune activă pentru acest număr de înmatriculare.", "error")
                return redirect(url_for("barrier_access"))

            # 1. Caută rezervare activă și validă acum (cu acces permis cu 15 minute înainte)
            cursor.execute("""
                SELECT pr.*, pz.zone_name
                FROM parking_reservations pr
                JOIN parking_zones pz ON pr.zone_id = pz.id
                WHERE pr.license_plate = %s
                  AND pr.status = 'active'
                  AND %s BETWEEN DATE_SUB(pr.reservation_start, INTERVAL 15 MINUTE) AND pr.reservation_end
                ORDER BY pr.reservation_start ASC
                LIMIT 1
            """, (license_plate, now))

            reservation = cursor.fetchone()

            if reservation:
                cursor.execute("""
                    UPDATE parking_zones
                    SET available_spots = available_spots - 1
                    WHERE id = %s AND available_spots > 0
                """, (reservation["zone_id"],))

                if cursor.rowcount == 0:
                    conn.rollback()
                    flash(f"Acces respins. Nu mai sunt locuri disponibile momentan în {reservation['zone_name']}.", "error")
                    return redirect(url_for("barrier_access"))

                cursor.execute("""
                    INSERT INTO parking_sessions
                    (user_id, zone_id, reservation_id, license_plate, start_time, status)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    reservation["user_id"],
                    reservation["zone_id"],
                    reservation["id"],
                    license_plate,
                    now,
                    "active"
                ))

                conn.commit()
                flash(f"Acces permis pe baza rezervării. Bariera s-a deschis pentru {reservation['zone_name']}.", "success")
                return redirect(url_for("barrier_access"))

            # 2. Verific dacă există rezervare expirată pentru număr
            cursor.execute("""
                SELECT pr.id, pr.status, pr.reservation_start, pr.reservation_end, pz.zone_name
                FROM parking_reservations pr
                JOIN parking_zones pz ON pr.zone_id = pz.id
                WHERE pr.license_plate = %s
                ORDER BY pr.created_at DESC
                LIMIT 1
            """, (license_plate,))
            latest_reservation = cursor.fetchone()

            if latest_reservation and latest_reservation["status"] == "expired":
                flash("Rezervarea pentru acest număr este expirată. Poți intra doar fără rezervare, dacă selectezi o zonă și există locuri.", "warning")

            # 3. Intrare fără rezervare
            if not selected_zone_id:
                flash("Nu există rezervare activă validă. Selectează o zonă pentru acces fără rezervare.", "error")
                return redirect(url_for("barrier_access"))

            cursor.execute("""
                SELECT id, zone_name, available_spots, status
                FROM parking_zones
                WHERE id = %s AND status = 'active'
                LIMIT 1
            """, (selected_zone_id,))
            zone = cursor.fetchone()

            if not zone:
                flash("Zona selectată nu este validă.", "error")
                return redirect(url_for("barrier_access"))

            cursor.execute("""
                UPDATE parking_zones
                SET available_spots = available_spots - 1
                WHERE id = %s AND available_spots > 0
            """, (zone["id"],))

            if cursor.rowcount == 0:
                conn.rollback()
                flash(f"Acces respins. Nu mai sunt locuri disponibile în {zone['zone_name']}.", "error")
                return redirect(url_for("barrier_access"))

            cursor.execute("""
                INSERT INTO parking_sessions
                (user_id, zone_id, reservation_id, license_plate, start_time, status)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                session["user_id"],
                zone["id"],
                None,
                license_plate,
                now,
                "active"
            ))

            conn.commit()
            flash(f"Acces permis fără rezervare. Bariera s-a deschis pentru {zone['zone_name']}.", "success")
            return redirect(url_for("barrier_access"))

    except mysql.connector.Error as err:
        if conn is not None:
            conn.rollback()
        flash(f"Eroare MySQL: {err}", "error")
        return redirect(url_for("home"))

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return render_template("barrier_access.html", parking_zones=parking_zones)

@app.route("/barrier-exit", methods=["GET", "POST"])
def barrier_exit():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = None
    cursor = None

    try:
        if request.method == "POST":
            license_plate = request.form.get("license_plate", "").strip().upper()

            if not license_plate:
                flash("Introdu numărul de înmatriculare.", "error")
                return redirect(url_for("barrier_exit"))

            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            now = datetime.now()

            cursor.execute("""
                SELECT 
                    ps.*,
                    pz.price_per_hour,
                    pz.zone_name,
                    pr.reservation_end,
                    pr.notes
                FROM parking_sessions ps
                JOIN parking_zones pz ON ps.zone_id = pz.id
                LEFT JOIN parking_reservations pr ON ps.reservation_id = pr.id
                WHERE ps.license_plate = %s
                  AND ps.status = 'active'
                ORDER BY ps.start_time ASC
                LIMIT 1
            """, (license_plate,))

            session_data = cursor.fetchone()

            if not session_data:
                flash("Nu există nicio sesiune activă pentru acest număr.", "error")
                return redirect(url_for("barrier_exit"))

            start_time = session_data["start_time"]
            price_per_hour = float(session_data["price_per_hour"])
            reservation_note = None

            if session_data["reservation_id"] is not None and session_data["reservation_end"] is not None:
                reservation_end = session_data["reservation_end"]

                if now <= reservation_end:
                    normal_hours = (now - start_time).total_seconds() / 3600
                    if normal_hours < 0:
                        normal_hours = 0
                    total_cost = round(normal_hours * price_per_hour, 2)
                else:
                    normal_end = reservation_end if reservation_end > start_time else start_time
                    normal_hours = (normal_end - start_time).total_seconds() / 3600
                    if normal_hours < 0:
                        normal_hours = 0

                    extra_hours = (now - reservation_end).total_seconds() / 3600
                    if extra_hours < 0:
                        extra_hours = 0

                    normal_cost = normal_hours * price_per_hour
                    extra_cost = extra_hours * (price_per_hour * 2)

                    total_cost = round(normal_cost + extra_cost, 2)
                    reservation_note = "Ieșire după expirarea rezervării - tarif extra aplicat."
            else:
                duration_hours = (now - start_time).total_seconds() / 3600
                if duration_hours < 0:
                    duration_hours = 0
                total_cost = round(duration_hours * price_per_hour, 2)

            cursor.execute("""
                UPDATE parking_sessions
                SET end_time = %s,
                    total_cost = %s,
                    status = 'finished'
                WHERE id = %s
            """, (now, total_cost, session_data["id"]))

            if session_data["reservation_id"] is not None:
                if reservation_note:
                    cursor.execute("""
                        UPDATE parking_reservations
                        SET status = 'completed',
                            notes = %s
                        WHERE id = %s
                    """, (reservation_note, session_data["reservation_id"]))
                else:
                    cursor.execute("""
                        UPDATE parking_reservations
                        SET status = 'completed'
                        WHERE id = %s
                    """, (session_data["reservation_id"],))

            cursor.execute("""
                UPDATE parking_zones
                SET available_spots = available_spots + 1
                WHERE id = %s AND available_spots < total_spots
            """, (session_data["zone_id"],))

            conn.commit()

            flash(
                f"Ieșire permisă din {session_data['zone_name']}. Cost total: {total_cost} lei.",
                "success"
            )
            return redirect(url_for("barrier_exit"))

    except mysql.connector.Error as err:
        flash(f"Eroare MySQL: {err}", "error")
        return redirect(url_for("home"))

    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return render_template("barrier_exit.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    conn = None
    cursor = None

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        if not full_name or not email or not password:
            flash("Completează toate câmpurile.", "error")
            return redirect(url_for("register"))

        password_hash = generate_password_hash(password)

        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            existing_user = cursor.fetchone()

            if existing_user:
                flash("Există deja un cont cu acest email.", "error")
                return redirect(url_for("register"))
            else:
                sql = """
                    INSERT INTO users (full_name, email, password_hash, role)
                    VALUES (%s, %s, %s, %s)
                """
                values = (full_name, email, password_hash, "user")
                cursor.execute(sql, values)
                conn.commit()

                flash("Cont creat cu succes. Te poți autentifica acum.", "success")
                return redirect(url_for("login"))

        except mysql.connector.Error as err:
            flash(f"Eroare MySQL: {err}", "error")
            return redirect(url_for("register"))

        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    conn = None
    cursor = None

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        if not email or not password:
            flash("Completează emailul și parola.", "error")
            return redirect(url_for("login"))

        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            sql = "SELECT * FROM users WHERE email = %s"
            cursor.execute(sql, (email,))
            user = cursor.fetchone()

            if user and check_password_hash(user["password_hash"], password):
                session["user_id"] = user["id"]
                session["full_name"] = user["full_name"]
                session["email"] = user["email"]
                session["role"] = user["role"]

                flash("Te-ai autentificat cu succes.", "success")
                return redirect(url_for("home"))
            else:
                flash("Email sau parolă greșită.", "error")
                return redirect(url_for("login"))

        except mysql.connector.Error as err:
            flash(f"Eroare MySQL: {err}", "error")
            return redirect(url_for("login"))

        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Te-ai delogat cu succes.", "success")
    return redirect(url_for("login"))


if __name__ == "__main__":
    print(app.url_map)
    app.run(host="0.0.0.0", port=5000, debug=True)