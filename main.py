from flask import Flask, request, jsonify
import google.generativeai as genai
import os

app = Flask(__name__)

# إعداد مفتاح الذكاء الاصطناعي (ستضعه غداً في إعدادات Railway)
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel('gemini-pro')

# هذا المتغير سيحفظ الكود الذي سيأخذه ماب روبلكس
current_luau_code = "print('السيرفر متصل، ننتظر أوامر الذكاء الاصطناعي...')"

@app.route('/ask-ai', methods=['POST'])
def ask_ai():
    global current_luau_code
    user_prompt = request.json.get("prompt")
    
    # نأمر الـ AI بكتابة كود روبلكس فقط بدون أي كلام بشري
    ai_prompt = f"اكتب كود Luau لروبلكس ينفذ هذا الطلب: {user_prompt}. اكتب الكود فقط بدون أي شرح أو علامات Markdown."
    
    try:
        response = model.generate_content(ai_prompt)
        # تنظيف الكود ليكون جاهزاً لروبلكس
        clean_code = response.text.replace('```lua', '').replace('```', '')
        current_luau_code = clean_code
        return jsonify({"status": "success", "message": "تم توليد الكود وإرساله للماب!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/roblox-fetch', methods=['GET'])
def roblox_fetch():
    # ماب روبلكس سيدخل على هذا الرابط كل ثانية ليأخذ الكود الجديد وينفذه
    return current_luau_code

if __name__ == '__main__':
    # تشغيل السيرفر على منفذ Railway
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
