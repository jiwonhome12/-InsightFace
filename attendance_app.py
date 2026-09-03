import cv2
import numpy as np
import threading
import time
import sqlite3
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from PIL import Image, ImageTk
from insightface.app import FaceAnalysis

DB_PATH = "faces.db"
THRESHOLD = 0.50

# ==========================================
# 1. DB 초기화 및 관련 함수
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 사용자 테이블 (비밀번호, 권한, 전공, 패널티 필드 추가)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT UNIQUE,
            password TEXT NOT NULL DEFAULT '1234',
            name TEXT NOT NULL,
            major TEXT NOT NULL DEFAULT '컴퓨터 공학 전공',
            role TEXT NOT NULL DEFAULT 'student', -- 'admin' 또는 'student'
            embedding BLOB NOT NULL,
            penalty INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 컬럼 존재 확인 및 마이그레이션
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN password TEXT NOT NULL DEFAULT '1234'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN major TEXT NOT NULL DEFAULT '컴퓨터 공학 전공'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'student'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN penalty INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # 출석 기록 테이블 (구분, 날짜, 시간 필드 보강)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attendance_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            name TEXT,
            log_type TEXT DEFAULT 'CHECK_IN', -- 'CHECK_IN' (출근) 또는 'CHECK_OUT' (퇴근)
            log_date TEXT,
            log_time TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)

    # 기본 관리자 계정 생성 (dongseo@mail.com / admin1234)
    cursor.execute("SELECT id FROM users WHERE user_id = 'dongseo@mail.com'")
    if not cursor.fetchone():
        dummy_embedding = np.zeros(512, dtype=np.float32).tobytes()
        cursor.execute("""
            INSERT INTO users (user_id, password, name, major, role, embedding, penalty)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ('dongseo@mail.com', 'admin1234', '관리자', '시스템관리', 'admin', dummy_embedding, 0))

    conn.commit()
    conn.close()

def load_users():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, name, major, penalty, embedding FROM users WHERE role = 'student'")
    rows = cursor.fetchall()
    conn.close()

    users = []
    for u_id, name, major, penalty, blob in rows:
        emb = np.frombuffer(blob, dtype=np.float32)
        users.append({
            "user_id": u_id, 
            "name": name, 
            "major": major, 
            "penalty": penalty, 
            "embedding": emb
        })
    return users

def get_weekly_attendance_stats(user_id):
    # 이번주 월요일 00:00:00부터 일요일 23:59:59까지의 출석 계산
    today = datetime.now()
    start_of_week = today - timedelta(days=today.weekday())
    start_date_str = start_of_week.strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT log_date, log_type, log_time 
        FROM attendance_logs 
        WHERE user_id = ? AND log_date >= ?
        ORDER BY log_date ASC, log_time ASC
    """, (user_id, start_date_str))
    logs = cursor.fetchall()
    conn.close()

    # 일자별로 IN / OUT 매칭
    days = {}
    for l_date, l_type, l_time in logs:
        if l_date not in days:
            days[l_date] = {}
        days[l_date][l_type] = l_time

    total_seconds = 0
    for date_str, types in days.items():
        if 'CHECK_IN' in types and 'CHECK_OUT' in types:
            try:
                t_in = datetime.strptime(f"{date_str} {types['CHECK_IN']}", "%Y-%m-%d %H:%M:%S")
                t_out = datetime.strptime(f"{date_str} {types['CHECK_OUT']}", "%Y-%m-%d %H:%M:%S")
                diff = (t_out - t_in).total_seconds()
                if diff > 0:
                    total_seconds += diff
            except Exception:
                pass

    total_hours = round(total_seconds / 3600.0, 1)
    return total_hours

def log_attendance(user_id, name, log_type):
    today_date = datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.now().strftime("%H:%M:%S")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    if log_type == 'CHECK_IN':
        cursor.execute("""
            SELECT id FROM attendance_logs 
            WHERE user_id = ? AND log_date = ? AND log_type = 'CHECK_IN'
        """, (user_id, today_date))
        if cursor.fetchone():
            conn.close()
            return False, "이미 오늘 출근 처리가 완료되었습니다."
            
    elif log_type == 'CHECK_OUT':
        cursor.execute("""
            SELECT id FROM attendance_logs 
            WHERE user_id = ? AND log_date = ? AND log_type = 'CHECK_OUT'
        """, (user_id, today_date))
        already_checked = cursor.fetchone()
        if already_checked:
            cursor.execute("""
                UPDATE attendance_logs SET log_time = ? 
                WHERE id = ?
            """, (now_time, already_checked[0]))
            conn.commit()
            conn.close()
            return True, f"{now_time} 퇴근이 확인 되었습니다."

    cursor.execute("""
        INSERT INTO attendance_logs (user_id, name, log_type, log_date, log_time)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, name, log_type, today_date, now_time))
    conn.commit()
    conn.close()

    type_str = "출근" if log_type == 'CHECK_IN' else "퇴근"
    return True, f"{now_time} {type_str}이 확인 되었습니다."


# ==========================================
# 2. UI 상태 프레임 정의
# ==========================================

class AdminLoginFrame(tk.Frame):
    """ 두번째 이미지: 기본 화면 (관리자 로그인 기능) """
    def __init__(self, parent):
        super().__init__(parent.right_container, bg="white", bd=1, relief=tk.SOLID)
        self.parent = parent
        
        # 외부 마진 시뮬레이션용 프레임
        card_inner = tk.Frame(self, bg="white")
        card_inner.pack(padx=30, pady=50, fill=tk.BOTH, expand=True)

        lbl_title = tk.Label(card_inner, text="관리자 로그인", font=("맑은 고딕", 20, "bold"), bg="white", fg="#1E293B")
        lbl_title.pack(pady=(0, 30))

        # 이메일 입력 그룹 (기입된 글자 없도록 비워둠)
        lbl_email = tk.Label(card_inner, text="이메일 주소", font=("맑은 고딕", 10, "bold"), bg="white", fg="#64748B")
        lbl_email.pack(anchor=tk.W, pady=(0, 4))
        self.ent_email = ttk.Entry(card_inner, font=("맑은 고딕", 12))
        self.ent_email.pack(fill=tk.X, ipady=4, pady=(0, 15))

        # 비밀번호 입력 그룹 (기입된 글자 없도록 비워둠)
        lbl_pwd = tk.Label(card_inner, text="비밀번호", font=("맑은 고딕", 10, "bold"), bg="white", fg="#64748B")
        lbl_pwd.pack(anchor=tk.W, pady=(0, 4))
        self.ent_pwd = ttk.Entry(card_inner, font=("맑은 고딕", 12), show="*")
        self.ent_pwd.pack(fill=tk.X, ipady=4, pady=(0, 25))

        # 로그인 버튼 해상도(크기) 강화
        btn_login = tk.Button(
            card_inner, text="로그인 (Log In)", bg="#3B82F6", fg="white", bd=0,
            activebackground="#2563EB", activeforeground="white",
            font=("맑은 고딕", 13, "bold"), height=2, cursor="hand2", command=self.try_login
        )
        btn_login.pack(fill=tk.X, pady=5)

    def try_login(self):
        email = self.ent_email.get().strip()
        pwd = self.ent_pwd.get().strip()

        if not (email and pwd):
            messagebox.showwarning("주의", "이메일과 비밀번호를 입력해주세요.")
            return

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT name, role FROM users WHERE user_id = ? AND password = ?", (email, pwd))
        row = cursor.fetchone()
        conn.close()

        if row:
            name, role = row
            if role == "admin":
                self.parent.set_admin_logged_in(True, email)
                self.ent_email.delete(0, tk.END)
                self.ent_pwd.delete(0, tk.END)
            else:
                messagebox.showerror("오류", "관리자 권한이 없습니다.")
        else:
            messagebox.showerror("오류", "로그인 정보가 틀렸습니다.")


class StudentInfoFrame(tk.Frame):
    """ 첫번째 이미지: 학생 얼굴 인식 시 뜨는 화면 (비밀번호 추가 검증 단계 포함) """
    def __init__(self, parent, student_info):
        super().__init__(parent.right_container, bg="white")
        self.parent = parent
        self.student_info = student_info

        # 이번주 출석 현황 박스 (Clean Card Style)
        stats_frame = tk.Frame(self, bg="#F8FAFC", bd=1, relief=tk.SOLID)
        stats_frame.pack(fill=tk.X, padx=15, pady=10)

        lbl_stats_title = tk.Label(stats_frame, text="이번주 출석 현황", font=("맑은 고딕", 13, "bold"), bg="#F8FAFC", fg="#1E293B")
        lbl_stats_title.pack(anchor=tk.W, padx=15, pady=(12, 8))

        weekly_hours = get_weekly_attendance_stats(student_info["user_id"])
        
        lbl_total = tk.Label(stats_frame, text=f"• total : {weekly_hours} 시간", font=("맑은 고딕", 11), bg="#F8FAFC", fg="#334155")
        lbl_total.pack(anchor=tk.W, padx=25, pady=2)

        # 3진 아웃제 기준 패널티 색상 경고 표기
        penalty_val = student_info['penalty']
        penalty_color = "#EF4444" if penalty_val >= 2 else "#F59E0B" if penalty_val == 1 else "#10B981"
        
        penalty_container = tk.Frame(stats_frame, bg="#F8FAFC")
        penalty_container.pack(anchor=tk.W, padx=25, pady=(2, 12))
        
        lbl_penalty_bullet = tk.Label(penalty_container, text="• 패널티 현황 : ", font=("맑은 고딕", 11), bg="#F8FAFC", fg="#334155")
        lbl_penalty_bullet.pack(side=tk.LEFT)
        
        lbl_penalty_value = tk.Label(penalty_container, text=f"{penalty_val}개", font=("맑은 고딕", 11, "bold"), bg="#F8FAFC", fg=penalty_color)
        lbl_penalty_value.pack(side=tk.LEFT)

        # 학생 프로필 정보 박스
        profile_frame = tk.Frame(self, bg="white", bd=1, relief=tk.SOLID)
        profile_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)

        name_masked = student_info["name"][0] + "o" * (len(student_info["name"]) - 1)

        info_inner = tk.Frame(profile_frame, bg="white")
        info_inner.pack(padx=20, pady=20, fill=tk.BOTH, expand=True)

        lbl_name_tag = tk.Label(info_inner, text="이름", font=("맑은 고딕", 9, "bold"), bg="white", fg="#94A3B8")
        lbl_name_tag.pack(anchor=tk.W, pady=(0, 1))
        lbl_name = tk.Label(info_inner, text=name_masked, font=("맑은 고딕", 14, "bold"), bg="white", fg="#0F172A")
        lbl_name.pack(anchor=tk.W, pady=(0, 12))

        lbl_id_tag = tk.Label(info_inner, text="학번", font=("맑은 고딕", 9, "bold"), bg="white", fg="#94A3B8")
        lbl_id_tag.pack(anchor=tk.W, pady=(0, 1))
        lbl_id = tk.Label(info_inner, text=student_info['user_id'], font=("맑은 고딕", 14, "bold"), bg="white", fg="#0F172A")
        lbl_id.pack(anchor=tk.W, pady=(0, 12))

        lbl_major_tag = tk.Label(info_inner, text="전공 학과", font=("맑은 고딕", 9, "bold"), bg="white", fg="#94A3B8")
        lbl_major_tag.pack(anchor=tk.W, pady=(0, 1))
        lbl_major = tk.Label(info_inner, text=student_info['major'], font=("맑은 고딕", 13, "bold"), bg="white", fg="#0F172A")
        lbl_major.pack(anchor=tk.W, pady=(0, 15))

        # 출근 / 퇴근 버튼 구성 (크기 강화)
        btn_frame = tk.Frame(info_inner, bg="white")
        btn_frame.pack(fill=tk.X, pady=5)

        btn_in = tk.Button(
            btn_frame, text="출 근 (IN)", bg="#10B981", fg="white", font=("맑은 고딕", 13, "bold"), 
            bd=0, activebackground="#059669", activeforeground="white", height=2, cursor="hand2",
            command=lambda: self.handle_action("CHECK_IN")
        )
        btn_in.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)

        btn_out = tk.Button(
            btn_frame, text="퇴 근 (OUT)", bg="#3B82F6", fg="white", font=("맑은 고딕", 13, "bold"), 
            bd=0, activebackground="#2563EB", activeforeground="white", height=2, cursor="hand2",
            command=lambda: self.handle_action("CHECK_OUT")
        )
        btn_out.pack(side=tk.RIGHT, expand=True, fill=tk.X, padx=5)

    def handle_action(self, log_type):
        # 도용 방지 비밀번호 확인 다이얼로그 띄우기
        pwd_input = simpledialog.askstring("도용 방지", "본인 확인을 위해 비밀번호를 입력해주세요:", show="*", parent=self)
        if pwd_input is None:
            return # 취소 시 동작 안 함
            
        # DB에서 저장된 본인 비밀번호와 매칭 검사
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT password FROM users WHERE user_id = ?", (self.student_info["user_id"],))
        row = cursor.fetchone()
        conn.close()

        if row and row[0] == pwd_input:
            success, msg = log_attendance(self.student_info["user_id"], self.student_info["name"], log_type)
            if success:
                messagebox.showinfo("확인 완료", msg)
            else:
                messagebox.showwarning("주의", msg)
            self.parent.reset_to_default_view()
        else:
            messagebox.showerror("오류", "비밀번호가 일치하지 않습니다. 도용 방지를 위해 요청을 중단합니다.")


class AdminDashboardFrame(tk.Frame):
    """ 세번째 이미지: 관리자 로그인시 화면 """
    def __init__(self, parent, admin_email):
        super().__init__(parent.right_container, bg="white")
        self.parent = parent
        self.admin_email = admin_email

        # 관리자 이메일 & 로그아웃 상단바
        top_bar = tk.Frame(self, bg="white")
        top_bar.pack(fill=tk.X, pady=8, padx=10)

        lbl_admin = tk.Label(top_bar, text=f"🔑 관리자: {self.admin_email}", font=("맑은 고딕", 11, "bold"), bg="white", fg="#334155")
        lbl_admin.pack(side=tk.LEFT, pady=5)

        btn_logout = tk.Button(
            top_bar, text="log out", bg="#EF4444", fg="white", font=("맑은 고딕", 10, "bold"),
            activebackground="#DC2626", activeforeground="white", bd=0, padx=14, pady=6, cursor="hand2",
            command=self.logout
        )
        btn_logout.pack(side=tk.RIGHT, pady=5)

        # 탭 뷰 스타일 커스텀
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.tab_students = ttk.Frame(self.notebook)
        self.tab_add_student = ttk.Frame(self.notebook)
        self.tab_logs = ttk.Frame(self.notebook)

        self.notebook.add(self.tab_students, text=" 학생 현황/수정 ")
        self.notebook.add(self.tab_add_student, text=" 학생 등록 ")
        self.notebook.add(self.tab_logs, text=" 전체 출결로그 ")

        self.build_students_tab()
        self.build_add_student_tab()
        self.build_logs_tab()

    def logout(self):
        self.parent.set_admin_logged_in(False)

    def build_students_tab(self):
        # 상단 리스트
        list_frame = ttk.LabelFrame(self.tab_students, text=" 학생 리스트 ")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        scroll = ttk.Scrollbar(list_frame)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree_students = ttk.Treeview(
            list_frame, columns=("id", "name", "major", "penalty"), show="headings", 
            yscrollcommand=scroll.set
        )
        self.tree_students.heading("id", text="학번")
        self.tree_students.heading("name", text="이름")
        self.tree_students.heading("major", text="전공")
        self.tree_students.heading("penalty", text="패널티")
        
        self.tree_students.column("id", width=95, anchor=tk.CENTER)
        self.tree_students.column("name", width=80, anchor=tk.CENTER)
        self.tree_students.column("major", width=130, anchor=tk.W)
        self.tree_students.column("penalty", width=65, anchor=tk.CENTER)
        self.tree_students.pack(fill=tk.BOTH, expand=True)
        scroll.config(command=self.tree_students.yview)

        self.tree_students.bind("<<TreeviewSelect>>", self.on_student_select)

        # 정보 수정 및 삭제 프레임
        control_frame = ttk.Frame(self.tab_students)
        control_frame.pack(fill=tk.X, padx=5, pady=5)

        # 선택정보 편집 폼
        form_frame = ttk.LabelFrame(control_frame, text=" 학생 데이터 편집 ")
        form_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5, pady=2)

        ttk.Label(form_frame, text="이름:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.edit_name = ttk.Entry(form_frame, width=9)
        self.edit_name.grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(form_frame, text="전공:").grid(row=0, column=2, padx=5, pady=5, sticky=tk.W)
        self.edit_major = ttk.Entry(form_frame, width=12)
        self.edit_major.grid(row=0, column=3, padx=5, pady=5)

        ttk.Label(form_frame, text="패널티:").grid(row=0, column=4, padx=5, pady=5, sticky=tk.W)
        self.edit_penalty = ttk.Entry(form_frame, width=4)
        self.edit_penalty.grid(row=0, column=5, padx=5, pady=5)

        btn_update = ttk.Button(form_frame, text="수정 완료", command=self.update_student)
        btn_update.grid(row=0, column=6, padx=8, pady=5)

        # 삭제 제어 영역 (크기 강화)
        btn_action_frame = ttk.Frame(control_frame)
        btn_action_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=5, pady=2)

        btn_del_sel = ttk.Button(btn_action_frame, text="선택 삭제", command=self.delete_selected)
        btn_del_sel.pack(fill=tk.X, ipady=4, pady=2)

        btn_del_all = ttk.Button(btn_action_frame, text="일괄 삭제", command=self.delete_all)
        btn_del_all.pack(fill=tk.X, ipady=4, pady=2)

        self.load_students()

    def build_add_student_tab(self):
        frame = ttk.LabelFrame(self.tab_add_student, text=" 학생 얼굴 및 상세 정보 등록 ")
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        grid_container = tk.Frame(frame, bg="#F8FAFC")
        grid_container.pack(padx=20, pady=20, fill=tk.BOTH, expand=True)

        ttk.Label(grid_container, text="학번(ID):", font=("맑은 고딕", 10, "bold"), background="#F8FAFC").grid(row=0, column=0, padx=10, pady=8, sticky=tk.W)
        self.add_id = ttk.Entry(grid_container, font=("맑은 고딕", 11), width=22)
        self.add_id.grid(row=0, column=1, padx=10, pady=8)

        ttk.Label(grid_container, text="비밀번호:", font=("맑은 고딕", 10, "bold"), background="#F8FAFC").grid(row=1, column=0, padx=10, pady=8, sticky=tk.W)
        self.add_pwd = ttk.Entry(grid_container, show="*", font=("맑은 고딕", 11), width=22)
        self.add_pwd.grid(row=1, column=1, padx=10, pady=8)

        ttk.Label(grid_container, text="이름:", font=("맑은 고딕", 10, "bold"), background="#F8FAFC").grid(row=2, column=0, padx=10, pady=8, sticky=tk.W)
        self.add_name = ttk.Entry(grid_container, font=("맑은 고딕", 11), width=22)
        self.add_name.grid(row=2, column=1, padx=10, pady=8)

        ttk.Label(grid_container, text="전공 학과:", font=("맑은 고딕", 10, "bold"), background="#F8FAFC").grid(row=3, column=0, padx=10, pady=8, sticky=tk.W)
        self.add_major = ttk.Entry(grid_container, font=("맑은 고딕", 11), width=22)
        self.add_major.grid(row=3, column=1, padx=10, pady=8)
        self.add_major.insert(0, "컴퓨터 공학 전공")

        # 등록 버튼 크기 강화
        btn_register = tk.Button(
            grid_container, text="💾 카메라 인식 얼굴로 등록", bg="#10B981", fg="white", bd=0,
            activebackground="#059669", activeforeground="white", font=("맑은 고딕", 12, "bold"),
            height=2, cursor="hand2", command=self.register_student
        )
        btn_register.grid(row=4, column=0, columnspan=2, pady=25, sticky=tk.EW)

    def build_logs_tab(self):
        # 상단 필터/검색 영역 바
        filter_frame = ttk.LabelFrame(self.tab_logs, text=" 기간 및 학생 검색 조건 ")
        filter_frame.pack(fill=tk.X, padx=5, pady=5)

        # 기본 기간을 오늘 기준으로 세팅
        today_str = datetime.now().strftime("%Y-%m-%d")
        week_ago_str = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        ttk.Label(filter_frame, text="시작일:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        self.ent_start_date = ttk.Entry(filter_frame, width=12)
        self.ent_start_date.insert(0, week_ago_str)
        self.ent_start_date.grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(filter_frame, text="종료일:").grid(row=0, column=2, padx=5, pady=5, sticky=tk.W)
        self.ent_end_date = ttk.Entry(filter_frame, width=12)
        self.ent_end_date.insert(0, today_str)
        self.ent_end_date.grid(row=0, column=3, padx=5, pady=5)

        ttk.Label(filter_frame, text="이름 검색:").grid(row=0, column=4, padx=5, pady=5, sticky=tk.W)
        self.ent_search_name = ttk.Entry(filter_frame, width=12)
        self.ent_search_name.grid(row=0, column=5, padx=5, pady=5)

        # 조회 버튼 크기 강화
        btn_search = tk.Button(
            filter_frame, text="🔍 조회하기", bg="#3B82F6", fg="white", font=("맑은 고딕", 11, "bold"),
            bd=0, activebackground="#2563EB", cursor="hand2", width=12, height=1
        )
        btn_search.grid(row=0, column=6, padx=15, pady=5)
        btn_search.config(command=self.load_logs_filtered)

        # 로그 트리뷰 목록
        list_container = tk.Frame(self.tab_logs)
        list_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        scroll = ttk.Scrollbar(list_container)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree_logs = ttk.Treeview(
            list_container, columns=("user_id", "name", "log_type", "log_date", "log_time"), show="headings",
            yscrollcommand=scroll.set
        )
        self.tree_logs.heading("user_id", text="학번")
        self.tree_logs.heading("name", text="이름")
        self.tree_logs.heading("log_type", text="구분")
        self.tree_logs.heading("log_date", text="날짜")
        self.tree_logs.heading("log_time", text="시간")
        
        self.tree_logs.column("user_id", width=100, anchor=tk.CENTER)
        self.tree_logs.column("name", width=80, anchor=tk.CENTER)
        self.tree_logs.column("log_type", width=70, anchor=tk.CENTER)
        self.tree_logs.column("log_date", width=110, anchor=tk.CENTER)
        self.tree_logs.column("log_time", width=110, anchor=tk.CENTER)
        self.tree_logs.pack(fill=tk.BOTH, expand=True)
        scroll.config(command=self.tree_logs.yview)

        self.load_logs()

    # DB 및 로드 제어 기능들
    def load_students(self):
        for item in self.tree_students.get_children():
            self.tree_students.delete(item)
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, name, major, penalty FROM users WHERE role = 'student'")
        for r in cursor.fetchall():
            self.tree_students.insert("", tk.END, values=r)
        conn.close()

    def load_logs(self):
        # 기본값 로드 (필터 없이 전체 출력)
        self.load_logs_filtered()

    def load_logs_filtered(self):
        for item in self.tree_logs.get_children():
            self.tree_logs.delete(item)

        start_date = self.ent_start_date.get().strip()
        end_date = self.ent_end_date.get().strip()
        search_name = self.ent_search_name.get().strip()

        # 값이 누락되었을 시 기본값 보강
        if not start_date: start_date = "1970-01-01"
        if not end_date: end_date = "2999-12-31"

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        if search_name:
            query = """
                SELECT user_id, name, log_type, log_date, log_time 
                FROM attendance_logs 
                WHERE name LIKE ? AND log_date BETWEEN ? AND ? 
                ORDER BY id DESC
            """
            cursor.execute(query, (f"%{search_name}%", start_date, end_date))
        else:
            query = """
                SELECT user_id, name, log_type, log_date, log_time 
                FROM attendance_logs 
                WHERE log_date BETWEEN ? AND ? 
                ORDER BY id DESC
            """
            cursor.execute(query, (start_date, end_date))

        for r in cursor.fetchall():
            display_type = "출근" if r[2] == 'CHECK_IN' else "퇴근"
            self.tree_logs.insert("", tk.END, values=(r[0], r[1], display_type, r[3], r[4]))
        conn.close()

    def on_student_select(self, event):
        selected = self.tree_students.selection()
        if not selected:
            return
        values = self.tree_students.item(selected[0], "values")
        
        self.edit_name.delete(0, tk.END)
        self.edit_name.insert(0, values[1])

        self.edit_major.delete(0, tk.END)
        self.edit_major.insert(0, values[2])

        self.edit_penalty.delete(0, tk.END)
        self.edit_penalty.insert(0, values[3])

    def update_student(self):
        selected = self.tree_students.selection()
        if not selected:
            messagebox.showwarning("선택 없음", "수정할 대상을 리스트에서 선택하세요.")
            return
        
        student_id = self.tree_students.item(selected[0], "values")[0]
        name = self.edit_name.get().strip()
        major = self.edit_major.get().strip()
        penalty_str = self.edit_penalty.get().strip()

        if not (name and major and penalty_str):
            messagebox.showwarning("경고", "수정 데이터를 모두 기입하세요.")
            return

        try:
            penalty = int(penalty_str)
        except ValueError:
            messagebox.showerror("오류", "패널티는 정수형만 입력 가능합니다.")
            return

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users SET name = ?, major = ?, penalty = ?
            WHERE user_id = ?
        """, (name, major, penalty, student_id))
        conn.commit()
        conn.close()

        messagebox.showinfo("수정 성공", "학생 정보 변경이 완료되었습니다.")
        self.load_students()
        self.parent.reload_users()

    def delete_selected(self):
        selected = self.tree_students.selection()
        if not selected:
            messagebox.showwarning("선택 없음", "삭제할 대상을 리스트에서 선택하세요.")
            return
        
        student_id = self.tree_students.item(selected[0], "values")[0]
        name = self.tree_students.item(selected[0], "values")[1]

        if messagebox.askyesno("삭제 확인", f"[{name}] 학생 정보를 완전히 삭제하시겠습니까?"):
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            # SQLite 타입 유연성을 고려하여 문자열 및 정수형 모두 삭제 쿼리 반영
            alt_id = int(student_id) if str(student_id).isdigit() else student_id
            cursor.execute("DELETE FROM users WHERE user_id = ? OR user_id = ?", (str(student_id), alt_id))
            cursor.execute("DELETE FROM attendance_logs WHERE user_id = ? OR user_id = ?", (str(student_id), alt_id))
            
            conn.commit()
            conn.close()

            messagebox.showinfo("성공", "선택 정보가 삭제되었습니다.")
            self.load_students()
            self.load_logs()
            self.parent.reload_users()

    def delete_all(self):
        if messagebox.askyesno("전체 삭제 경고", "모든 학생 정보 및 모든 출결 이력이 영구 삭제됩니다. 진행하시겠습니까?"):
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE role = 'student'")
            cursor.execute("DELETE FROM attendance_logs")
            conn.commit()
            conn.close()

            messagebox.showinfo("성공", "전체 데이터가 삭제되었습니다.")
            self.load_students()
            self.load_logs()
            self.parent.reload_users()

    def register_student(self):
        s_id = self.add_id.get().strip()
        pwd = self.add_pwd.get().strip()
        name = self.add_name.get().strip()
        major = self.add_major.get().strip()

        if not (s_id and pwd and name and major):
            messagebox.showwarning("입력 미달", "모든 정보를 입력하세요.")
            return

        if not self.parent.cached_faces:
            messagebox.showerror("얼굴 인식 실패", "카메라 영역에 등록할 학생의 얼굴이 감지되지 않았습니다.")
            return

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE user_id = ?", (s_id,))
        if cursor.fetchone():
            messagebox.showerror("오류", "이미 가입된 학번 ID입니다.")
            conn.close()
            return

        emb = self.parent.cached_faces[0].embedding.tobytes()
        cursor.execute("""
            INSERT INTO users (user_id, password, name, major, role, embedding, penalty)
            VALUES (?, ?, ?, ?, ?, ?, 0)
        """, (s_id, pwd, name, major, 'student', emb))
        conn.commit()
        conn.close()

        messagebox.showinfo("성공", f"[{name}] 학생이 성공적으로 등록되었습니다.")
        self.add_id.delete(0, tk.END)
        self.add_pwd.delete(0, tk.END)
        self.add_name.delete(0, tk.END)
        
        self.load_students()
        self.parent.reload_users()


# ==========================================
# 3. 메인 어플리케이션
# ==========================================
class AttendanceApp:
    def __init__(self, window):
        self.window = window
        self.window.title("창의공간 얼굴인식 출석체크 시스템")
        self.window.geometry("1100x640")
        self.window.configure(bg="#F1F5F9") # 깔끔한 slate 연회색 배경

        # UI 스타일 테마 통합 구성
        style = ttk.Style()
        style.theme_use('clam')
        
        # Notebook (Tabs) 스타일링
        style.configure('TNotebook', background='#F1F5F9', borderwidth=0)
        style.configure('TNotebook.Tab', background='#E2E8F0', foreground='#475569', padding=[15, 6], font=('맑은 고딕', 10, 'bold'))
        style.map('TNotebook.Tab', background=[('selected', '#ffffff')], foreground=[('selected', '#1E293B')])
        
        # Treeview 스타일링
        style.configure('Treeview', background='#ffffff', fieldbackground='#ffffff', rowheight=28, font=('맑은 고딕', 9))
        style.configure('Treeview.Heading', background='#E2E8F0', foreground='#1E293B', font=('맑은 고딕', 10, 'bold'))
        style.map('Treeview', background=[('selected', '#3B82F6')], foreground=[('selected', '#ffffff')])
        
        # LabelFrame 스타일링
        style.configure('TLabelframe', background='#ffffff', bordercolor='#CBD5E1', borderwidth=1)
        style.configure('TLabelframe.Label', background='#ffffff', foreground='#1E293B', font=('맑은 고딕', 10, 'bold'))

        # Ttk Button 스타일링 크기 및 패딩 조절로 해상도 대폭 업그레이드
        style.configure('TButton', font=('맑은 고딕', 11, 'bold'), padding=8)

        # DB 초기화
        init_db()

        # InsightFace 초기화
        self.app = FaceAnalysis(
            name="buffalo_l",
            allowed_modules=['detection', 'recognition'],
            providers=["CPUExecutionProvider"]
        )
        self.app.prepare(ctx_id=0, det_size=(256, 256))
        self.enrolled_users = load_users()

        # 상태 제어 필드
        self.admin_logged_in = False
        self.admin_email = ""
        self.current_view_state = None  # "login", "student", "admin"
        self.current_student_id = None
        self.last_face_time = 0.0

        # UI 레이아웃 구성
        self.create_widgets()

        # 기본 화면으로 전환
        self.reset_to_default_view()

        # 웹캠 기동
        self.cap = cv2.VideoCapture(0)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        self.latest_frame = None
        self.cached_faces = []
        self.is_running = True

        # 비디오 리프레시 및 추론 백그라운드
        self.ai_thread = threading.Thread(target=self.ai_worker, daemon=True)
        self.ai_thread.start()

        self.update_video()

    def create_widgets(self):
        # 상단 타이틀 배너 (Modern Dark Slate)
        title_frame = tk.Frame(self.window, bg="#0F172A", height=80)
        title_frame.pack(fill=tk.X, side=tk.TOP)
        title_frame.pack_propagate(False)
        
        lbl_title = tk.Label(title_frame, text="창의공간 얼굴인식 출석체크 시스템", bg="#0F172A", fg="white", font=("맑은 고딕", 20, "bold"))
        lbl_title.pack(pady=20)

        # 메인 콘텐츠 컨테이너
        content_frame = tk.Frame(self.window, bg="#F1F5F9")
        content_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # 좌측: 카메라 패널
        self.camera_panel = tk.Frame(content_frame, bd=1, relief=tk.SOLID, bg="#0F172A")
        self.camera_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=False)
        
        self.video_label = tk.Label(self.camera_panel, bg="#0F172A")
        self.video_label.pack(padx=10, pady=10)

        # 우측: 가변 상태 패널 컨테이너
        self.right_container = tk.Frame(content_frame, width=440, bg="#F1F5F9")
        self.right_container.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(20, 0))
        self.right_container.pack_propagate(False)

    def ai_worker(self):
        while self.is_running:
            if self.latest_frame is not None:
                # 관리자 탭에서도 학생 신규 등록을 위해 인물 스캔 연산은 항상 유지
                frame_to_process = self.latest_frame.copy()
                faces = self.app.get(frame_to_process)
                self.cached_faces = faces
            time.sleep(0.04)

    def update_video(self):
        ret, frame = self.cap.read()
        if ret:
            frame = cv2.flip(frame, 1)
            self.latest_frame = frame
            display = frame.copy()

            best_user = None
            best_sim = -1.0

            # 얼굴 바운딩 박스 렌더링
            for face in self.cached_faces:
                bbox = face.bbox.astype(int)
                emb = face.embedding
                
                for u in self.enrolled_users:
                    sim = np.dot(emb, u["embedding"]) / (np.linalg.norm(emb) * np.linalg.norm(u["embedding"]))
                    if sim > best_sim:
                        best_sim = sim
                        best_user = u

                # 테두리 및 텍스트 렌더링을 깔끔하게 개선
                if best_sim >= THRESHOLD and best_user:
                    label = f"{best_user['name']} ({best_sim:.2f})"
                    color = (16, 185, 129) # Neon Green (#10B981)
                else:
                    label = "UNKNOWN"
                    color = (239, 68, 68) # Rose Red (#EF4444)

                cv2.rectangle(display, (bbox[0], bbox[1]), (bbox[2], bbox[3]), color, 2)
                cv2.putText(display, label, (bbox[0], max(20, bbox[1] - 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

            # 관리자가 로그인하지 않았을 때의 학생 카드 전환 로직
            if not self.admin_logged_in:
                if best_sim >= THRESHOLD and best_user:
                    self.last_face_time = time.time()
                    # 새로운 사용자를 보았을 때 화면 카드 교환
                    if self.current_student_id != best_user["user_id"]:
                        self.current_student_id = best_user["user_id"]
                        self.show_student_view(best_user)
                else:
                    # 감지된 얼굴이 없으면 4초 카운트 다운 후 기본 관리자 로그인 카드로 복귀
                    if self.current_view_state == "student":
                        if time.time() - self.last_face_time > 4.0:
                            self.reset_to_default_view()

            cv2image = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(cv2image)
            imgtk = ImageTk.PhotoImage(image=img)
            self.video_label.imgtk = imgtk
            self.video_label.configure(image=imgtk)

        if self.is_running:
            self.window.after(20, self.update_video)

    def reload_users(self):
        self.enrolled_users = load_users()

    def show_student_view(self, student_info):
        self.clear_right_container()
        self.current_view_state = "student"
        
        frame = StudentInfoFrame(self, student_info)
        frame.pack(fill=tk.BOTH, expand=True)

    def reset_to_default_view(self):
        self.clear_right_container()
        self.current_student_id = None
        self.current_view_state = "login"
        
        frame = AdminLoginFrame(self)
        frame.pack(fill=tk.BOTH, expand=True)

    def set_admin_logged_in(self, logged_in, admin_email=""):
        self.admin_logged_in = logged_in
        self.admin_email = admin_email
        self.clear_right_container()

        if logged_in:
            self.current_view_state = "admin"
            frame = AdminDashboardFrame(self, admin_email)
            frame.pack(fill=tk.BOTH, expand=True)
        else:
            self.reset_to_default_view()

    def clear_right_container(self):
        for widget in self.right_container.winfo_children():
            widget.destroy()

    def on_close(self):
        self.is_running = False
        self.cap.release()
        self.window.destroy()


# ==========================================
# 4. 앱 실행
# ==========================================
if __name__ == "__main__":
    root = tk.Tk()
    app = AttendanceApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()