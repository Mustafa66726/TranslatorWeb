from flask import Flask, render_template, request, jsonify, send_file, Response
from deep_translator import GoogleTranslator
import pymupdf as fitz  # PyMuPDF
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

class PDFElement:
    def __init__(self, type, content, rect, font_size=None, font_name=None, color=None):
        self.type = type  # 'text', 'image', 'table', etc.
        self.content = content
        self.rect = rect  # (x0, y0, x1, y1)
        self.font_size = font_size
        self.font_name = font_name
        self.color = color

def extract_pdf_elements(pdf_path):
    """استخراج جميع عناصر PDF مع تحسين الأداء"""
    doc = fitz.open(pdf_path)
    elements = []
    
    for page_num in range(len(doc)):
        page = doc[page_num]
        
        # استخراج النص بشكل أكثر كفاءة
        text_blocks = page.get_text("blocks")
        for block in text_blocks:
            if block[6] == 0:  # نص عادي
                elements.append(PDFElement(
                    'text',
                    block[4],
                    (block[0], block[1], block[2], block[3]),
                    font_size=block[5]
                ))
        
        # استخراج الصور
        images = page.get_images(full=True)
        for img_index, img_info in enumerate(images):
            xref = img_info[0]
            base_image = doc.extract_image(xref)
            elements.append(PDFElement(
                'image',
                base_image["image"],
                page.get_image_bbox(img_info),
            ))
            
    doc.close()
    return elements

def create_translated_pdf(output_path, original_path, translated_elements, target_lang='ar'):
    """إنشاء PDF مترجم مع الحفاظ على التنسيق الأصلي"""
    try:
        # إنشاء PDF جديد
        doc = fitz.open()
        original_doc = fitz.open(original_path)
        
        # نسخ الصفحات من الملف الأصلي
        doc.insert_pdf(original_doc)
        
        # معالجة كل عنصر مترجم
        for element in translated_elements:
            if element.type == 'text':
                # تحديد الصفحة المناسبة
                page = doc[0]  # نفترض أن كل العناصر في الصفحة الأولى حالياً
                
                # إضافة النص المترجم
                text_rect = fitz.Rect(element.rect)
                # مسح المنطقة القديمة
                page.draw_rect(text_rect, color=None, fill=(1, 1, 1))
                
                # إضافة النص الجديد
                text = element.content
                if target_lang == 'ar':
                    text = handle_arabic_text(text)
                
                # تعيين حجم الخط والنمط
                font_size = element.font_size if element.font_size else 11
                
                # إضافة النص مع تحديد ملف الخط
                page.insert_text(
                    text_rect.tl,  # النقطة العلوية اليسرى
                    text,
                    fontfile=ARABIC_FONT_PATH,
                    fontsize=font_size,
                    color=element.color if element.color else (0, 0, 0)
                )
            
            elif element.type == 'image':
                # معالجة الصور إذا لزم الأمر
                pass
        
        # حفظ الملف النهائي
        doc.save(output_path)
        doc.close()
        original_doc.close()
        return True
        
    except Exception as e:
        print(f"Error creating PDF: {str(e)}")
        traceback.print_exc()
        return False

def process_pdf_translation(input_path, target_lang, task_id):
    """معالجة ترجمة PDF مع التحسينات الجديدة"""
    try:
        # استخراج العناصر
        elements = extract_pdf_elements(input_path)
        total_elements = len([e for e in elements if e.type == 'text'])
        processed = 0
        
        # تجميع النصوص للترجمة المتوازية
        text_elements = [e for e in elements if e.type == 'text']
        chunks = []
        for element in text_elements:
            element_chunks = chunk_text(element.content)
            chunks.extend(element_chunks)
        
        # ترجمة متوازية للنصوص
        with ThreadPoolExecutor(max_workers=min(10, len(chunks))) as executor:
            translations_future = {executor.submit(translate_chunk, chunk, target_lang): chunk for chunk in chunks}
            
            # تجميع الترجمات
            translations = {}
            for future in translations_future:
                original = translations_future[future]
                translation = future.result()
                translations[original] = translation
                processed += 1
                progress = (processed / total_elements) * 100
                update_progress(task_id, progress)
        
        # إنشاء العناصر المترجمة
        translated_elements = []
        for element in elements:
            if element.type == 'text':
                translated_content = translations.get(element.content, element.content)
                translated_elements.append(PDFElement(
                    element.type,
                    translated_content,
                    element.rect,
                    element.font_size,
                    element.font_name,
                    element.color
                ))
            else:
                translated_elements.append(element)
        
        # إنشاء ملف PDF المترجم
        output_path = os.path.join(app.config['UPLOAD_FOLDER'], f'translated_{task_id}.pdf')
        create_translated_pdf(output_path, input_path, translated_elements, target_lang)
        
        update_progress(task_id, 100, "اكتملت الترجمة")
        return output_path
        
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
