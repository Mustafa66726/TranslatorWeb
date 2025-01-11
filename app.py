from flask import Flask, render_template, request, jsonify, send_file, Response
from deep_translator import GoogleTranslator
from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.units import inch, cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Frame
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
import arabic_reshaper
from bidi.algorithm import get_display
import os
import traceback
from werkzeug.utils import secure_filename
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import json
import re
import io

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# تخزين حالة التقدم
translation_progress = {}
translation_lock = threading.Lock()

# تخزين مؤقت للترجمات
translation_cache = {}
cache_lock = threading.Lock()

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

# تعريف مسار الخط
ARABIC_FONT_PATH = os.path.join(os.path.dirname(__file__), 'fonts', 'arial.ttf')
FONTS_DIR = os.path.join(os.path.dirname(__file__), 'fonts')

# إنشاء مجلد الخطوط إذا لم يكن موجوداً
if not os.path.exists(FONTS_DIR):
    os.makedirs(FONTS_DIR)

# التحقق من وجود ملف الخط
def ensure_font_file():
    if not os.path.exists(ARABIC_FONT_PATH):
        windows_font = "C:/Windows/Fonts/arial.ttf"
        if os.path.exists(windows_font):
            import shutil
            shutil.copy2(windows_font, ARABIC_FONT_PATH)
        else:
            raise Exception("ملف الخط غير موجود")

ensure_font_file()

pdfmetrics.registerFont(TTFont('Arabic', ARABIC_FONT_PATH))

def allowed_file(filename):
    """التحقق من أن الملف هو PDF"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() == 'pdf'

def update_progress(task_id, progress, status="جاري العمل"):
    with translation_lock:
        translation_progress[task_id] = {
            'progress': progress,
            'status': status
        }

def get_progress(task_id):
    with translation_lock:
        return translation_progress.get(task_id, {'progress': 0, 'status': 'جاري التحميل'})

def get_cached_translation(text, target_lang):
    """الحصول على الترجمة من الذاكرة المؤقتة"""
    cache_key = f"{text}:{target_lang}"
    with cache_lock:
        return translation_cache.get(cache_key)

def cache_translation(text, translation, target_lang):
    """تخزين الترجمة في الذاكرة المؤقتة"""
    cache_key = f"{text}:{target_lang}"
    with cache_lock:
        translation_cache[cache_key] = translation

def translate_chunk(text, target_lang):
    """ترجمة جزء من النص مع استخدام التخزين المؤقت"""
    if not text.strip():
        return text
        
    cached = get_cached_translation(text, target_lang)
    if cached:
        return cached
        
    try:
        translator = GoogleTranslator(source='auto', target=target_lang)
        translation = translator.translate(text)
        if translation:
            cache_translation(text, translation, target_lang)
            return translation
    except Exception as e:
        print(f"خطأ في الترجمة: {str(e)}")
    return text

def chunk_text(text, chunk_size=1000):
    """تقسيم النص إلى أجزاء مع الحفاظ على سلامة الجمل"""
    sentences = re.split(r'([.!?।\n])', text)
    chunks = []
    current_chunk = ""
    
    for sentence in sentences:
        if len(current_chunk) + len(sentence) <= chunk_size:
            current_chunk += sentence
        else:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = sentence
            
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks

def handle_arabic_text(text):
    try:
        if not isinstance(text, str):
            return str(text)
        reshaped_text = arabic_reshaper.reshape(text)
        bidi_text = get_display(reshaped_text)
        return bidi_text
    except Exception as e:
        print(f"Error in handle_arabic_text: {str(e)}")
        return text

def extract_text_from_pdf(pdf_path):
    """استخراج النص من ملف PDF"""
    text_content = []
    try:
        reader = PdfReader(pdf_path)
        for page in reader.pages:
            text = page.extract_text()
            if text.strip():
                text_content.append(text)
    except Exception as e:
        print(f"خطأ في استخراج النص: {str(e)}")
    return text_content

def create_translated_pdf(output_path, texts, target_lang='ar'):
    """إنشاء PDF مترجم"""
    try:
        doc = SimpleDocTemplate(
            output_path,
            pagesize=letter,
            rightMargin=72,
            leftMargin=72,
            topMargin=72,
            bottomMargin=72
        )

        story = []
        styles = getSampleStyleSheet()
        arabic_style = ParagraphStyle(
            'Arabic',
            parent=styles['Normal'],
            fontName='Arabic',
            fontSize=12,
            leading=14,
            alignment=1 if target_lang == 'ar' else 0
        )

        for text in texts:
            p = Paragraph(text, arabic_style)
            story.append(p)
            story.append(Spacer(1, 12))

        doc.build(story)
        return True
    except Exception as e:
        print(f"خطأ في إنشاء PDF: {str(e)}")
        return False

def process_pdf_translation(input_path, target_lang, task_id):
    """معالجة ترجمة PDF"""
    try:
        # استخراج النصوص
        texts = extract_text_from_pdf(input_path)
        total_texts = len(texts)
        translated_texts = []
        
        # ترجمة النصوص
        for i, text in enumerate(texts):
            translated = translate_chunk(text, target_lang)
            if target_lang == 'ar':
                translated = handle_arabic_text(translated)
            translated_texts.append(translated)
            progress = ((i + 1) / total_texts) * 100
            update_progress(task_id, progress)
        
        # إنشاء PDF المترجم
        output_path = os.path.join(app.config['UPLOAD_FOLDER'], f'translated_{task_id}.pdf')
        if create_translated_pdf(output_path, translated_texts, target_lang):
            update_progress(task_id, 100, "اكتملت الترجمة")
            return output_path
        else:
            raise Exception("فشل في إنشاء ملف PDF المترجم")
            
    except Exception as e:
        print(f"خطأ في معالجة الملف: {str(e)}")
        traceback.print_exc()
        update_progress(task_id, 0, "حدث خطأ")
        return None

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/translate', methods=['POST'])
def translate_text_route():
    try:
        data = request.get_json()
        text = data.get('text', '')
        target_lang = data.get('target_lang', 'ar')

        if not text:
            return jsonify({'error': 'No text provided'}), 400

        translated_text = translate_chunk(text, target_lang)
        
        return jsonify({
            'translated_text': translated_text,
            'target_lang': target_lang
        })
    except Exception as e:
        print(f"Error in translate_text_route: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/translate-pdf', methods=['POST'])
def translate_pdf_file():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'لم يتم تحديد ملف'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'لم يتم اختيار ملف'}), 400
        
        if not allowed_file(file.filename):
            return jsonify({'error': 'نوع الملف غير مسموح به. يرجى رفع ملفات PDF فقط.'}), 400

        target_lang = request.form.get('target_lang', 'ar')
        task_id = str(int(time.time() * 1000))
        
        filename = secure_filename(file.filename)
        input_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(input_path)

        def process_translation():
            output_path = process_pdf_translation(input_path, target_lang, task_id)
            if output_path:
                try:
                    os.remove(input_path)
                except:
                    pass

        translation_thread = threading.Thread(target=process_translation)
        translation_thread.start()

        return jsonify({
            'task_id': task_id,
            'message': 'بدأت عملية الترجمة'
        })

    except Exception as e:
        print(f"Error in translate_pdf_file: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': f'حدث خطأ أثناء ترجمة الملف: {str(e)}'}), 500

@app.route('/translation-progress/<task_id>')
def check_progress(task_id):
    progress_info = get_progress(task_id)
    return jsonify(progress_info)

@app.route('/download-translation/<task_id>')
def download_translation(task_id):
    try:
        filename = f'translated_{task_id}.pdf'
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        if os.path.exists(file_path):
            return send_file(file_path, as_attachment=True, download_name='translated_document.pdf')
        else:
            return jsonify({'error': 'الملف غير موجود'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
