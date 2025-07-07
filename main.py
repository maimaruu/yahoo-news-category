import time
import re
import json
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from oauth2client.service_account import ServiceAccountCredentials
import gspread

# --- Google Sheets設定 ---
SERVICE_ACCOUNT_FILE = "credentials.json"
SHEET_NAME = "yahoo-news-scraper" # スプレッドシート名を変更
# シート名はデフォルトで最初のシート（sheet1）が使用されます。

# --- Selenium設定 ---
def init_driver():
    """
    WebDriverを初期化し、ヘッドレスモードでChromeを設定します。
    GitHub Actionsの環境に合わせてパスやオプションを設定します。
    """
    chrome_options = Options()
    chrome_options.add_argument("--headless")           # ヘッドレスモードで実行 (GUIなし)
    chrome_options.add_argument("--no-sandbox")         # サンドボックスモードを無効化 (Docker/CI環境で必要)
    chrome_options.add_argument("--disable-dev-shm-usage") # /dev/shm の使用を無効化 (メモリが少ない環境で必要)
    chrome_options.add_argument("--window-size=1920,1080") # ウィンドウサイズを設定
    chrome_options.add_argument("--disable-gpu")        # GPUを無効化 (一部環境で必要)
    chrome_options.add_argument("--disable-extensions") # 拡張機能を無効化
    chrome_options.add_argument("--proxy-server='direct://'") # プロキシサーバーを直接接続に設定
    chrome_options.add_argument("--proxy-bypass-list=*") # プロキシバイパスリスト
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")

    # GitHub Actions上のChromedriverのパス
    service = Service("/usr/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=chrome_options)
    print("[DEBUG] WebDriver initialized.")
    return driver

# --- 本文抽出関数 ---
def extract_body(soup):
    """
    BeautifulSoupオブジェクトから記事の本文を抽出します。
    不要な要素（画像、広告、スクリプトなど）を除去します。
    """
    # 記事本文のコンテナを検索 (Yahoo!ニュースのクラス名に合わせて調整)
    # 現在のYahoo!ニュースでは `article.sc-54nboa-0` のような構造が多い
    article_content_div = soup.find("div", class_="sc-54nboa-0")
    if not article_content_div:
        # 別の可能性のあるセレクタも試す
        article_content_div = soup.find("div", class_=re.compile(r"article_body|ArticleBody|yjSlinkDirectlink"))

    if not article_content_div:
        print("[DEBUG] No article content container found.")
        return ""

    # 不要なタグを除去
    for tag in article_content_div.find_all(["figure", "aside", "script", "style", "noscript", "blockquote"]):
        tag.decompose()

    # 段落を結合
    paragraphs = [p.get_text(" ", strip=True) for p in article_content_div.find_all("p") if p.get_text(strip=True)]
    body = "\n".join(paragraphs)
    print(f"[DEBUG] Extracted body part length: {len(body)}")
    return body

# --- 記事情報取得関数 ---
def extract_article_info(driver, url):
    """
    記事のURLにアクセスし、タイトル、情報源、掲載時刻、本文、ジャンル、IDを抽出します。
    """
    try:
        print(f"[DEBUG] Extracting info from: {url}")
        driver.get(url)
        time.sleep(2) # ページが完全に読み込まれるのを待つ
        soup = BeautifulSoup(driver.page_source, "html.parser")

        # 1. IDの抽出
        # URLから直接記事IDを抽出
        article_id_match = re.search(r'articles/([a-f0-9]+)', url)
        article_id = article_id_match.group(1) if article_id_match else "NO_ID"
        print(f"[DEBUG] Article ID: {article_id}")

        # 2. タイトルの抽出
        meta_title = soup.find("meta", property="og:title")
        title = meta_title["content"].strip() if meta_title and meta_title.get("content") else "NO TITLE"
        # 「（情報源） - Yahoo!ニュース」の部分を削除
        title = re.sub(r'（.*?） - Yahoo!ニュース$', '', title).strip()
        print(f"[DEBUG] Title: {title}")

        # 3. 情報源の抽出 - 修正されたロジック
        provider = "不明" # Default

        # 1. ld+jsonのauthor.nameを最優先で試す
        ld_json = soup.find("script", type="application/ld+json")
        if ld_json:
            try:
                data = json.loads(ld_json.string)
                if isinstance(data, dict):
                    if "author" in data and "name" in data["author"]:
                        provider = data["author"]["name"].strip()
                        print(f"[DEBUG] Provider (ld+json author): {provider}")
                    elif "publisher" in data and "name" in data["publisher"] and data["publisher"]["name"].strip() != "Yahoo!ニュース":
                        # publisherがYahoo!ニュース以外なら採用
                        provider = data["publisher"]["name"].strip()
                        print(f"[DEBUG] Provider (ld+json publisher, non-Yahoo): {provider}")
            except json.JSONDecodeError as e:
                print(f"[DEBUG] Failed to parse ld+json for provider: {e}")

        # 2. ld+jsonで取得できなかった場合、metaタグのauthor/publisherを試す
        if provider == "不明":
            meta_author = soup.find("meta", attrs={"name": re.compile("author|publisher", re.I)})
            if meta_author and meta_author.get("content"):
                provider = meta_author["content"].strip()
                print(f"[DEBUG] Provider (meta_author): {provider}")

        # 3. それでも不明な場合、記事下部のプロバイダー情報を試す (元のコードから維持)
        if provider == "不明":
            provider_span = soup.find("span", class_="sc-f06b9b1-0") # ニュース提供元のクラス名
            if provider_span:
                provider = provider_span.get_text(strip=True)
                print(f"[DEBUG] Provider (fallback span): {provider}")

        print(f"[DEBUG] Final Provider: {provider}")


        # 4. 掲載時刻の抽出
        pub_time = ""
        time_tag = soup.find("time")
        if time_tag and time_tag.has_attr('datetime'):
            pub_time = time_tag['datetime'].strip()
        elif time_tag:
            pub_time = time_tag.get_text(strip=True)
        # Fallback to meta tag if time tag not found or empty
        if not pub_time:
            meta_pubdate = soup.find("meta", attrs={"name": "pubdate"})
            if meta_pubdate and meta_pubdate.get("content"):
                pub_time = meta_pubdate["content"].strip()

        print(f"[DEBUG] Published Time: {pub_time}")

        # 5. ジャンル（カテゴリ）の抽出
        genre = "その他" # デフォルトジャンル
        # 主要カテゴリのマッピング辞書
        CATEGORY_MAP = {
            "dom": "国内",
            "wor": "国際",
            "bus": "経済",
            "eco": "経済",
            "ent": "エンタメ",
            "spo": "スポーツ",
            "it": "IT",
            "sci": "科学",
            "life": "ライフ",
            "loc": "地域",
            "main": "主要"
        }

        # サブカテゴリのマッピング辞書
        SUBCATEGORY_MAP = {
            "poli": "政治",
            "soci": "社会",
            "peo": "人", # 人物
            "oversea": "国際総合", # URLで oversea が使われている場合
            "chn": "中国・台湾",
            "kor": "韓国・北朝鮮",
            "asia": "アジア・オセアニア",
            "na": "北米",
            "ca": "中南米",
            "eu": "ヨーロッパ",
            "mea": "中東・アフリカ",
            "biz": "経済総合", # URLで biz が使われている場合
            "mkt": "市況",
            "stk": "株式",
            "ind": "産業",
            "mus": "音楽",
            "mov": "映画",
            "game": "ゲーム",
            "korasian": "アジア・韓流",
            "base": "野球",
            "soc": "サッカー",
            "moto": "モータースポーツ",
            "horse": "競馬",
            "golf": "ゴルフ",
            "fig": "格闘技",
            "health": "ヘルス",
            "env": "環境",
            "art": "文化・アート",
            "tohoku": "北海道・東北",
            "kant": "関東",
            "shinetu": "信越・北陸",
            "tokai": "東海",
            "kinki": "近畿",
            "chugoku": "中国",
            "shikoku": "四国",
            "kushu": "九州・沖縄",
            "itpro": "製品", # ITの製品カテゴリ
            "it総合": "IT総合", # 明示的にIT総合とする場合
            "sci総合": "科学総合", # 明示的に科学総合とする場合
            "life総合": "ライフ総合", # 明示的にライフ総合とする場合
            "ent総合": "エンタメ総合", # 明示的にエンタメ総合とする場合
            "spo総合": "スポーツ総合", # 明示的にスポーツ総合とする場合
            "dom総合": "国内総合", # 明示的に国内総合とする場合
            "wor総合": "国際総合", # 明示的に国際総合とする場合
            "eco総合": "経済総合", # 明示的に経済総合とする場合
            "loc総合": "地域総合" # 明示的に地域総合とする場合
        }

        # __PRELOADED_STATE__ からジャンルを抽出 (最優先)
        preloaded_state_script_content = None
        for script_tag in soup.find_all("script"):
            if script_tag.string and 'window.__PRELOADED_STATE__ =' in script_tag.string:
                preloaded_state_script_content = script_tag.string
                break

        if preloaded_state_script_content:
            json_start = preloaded_state_script_content.find('{')
            json_end = preloaded_state_script_content.rfind('}')

            if json_start != -1 and json_end != -1 and json_end > json_start:
                json_str = preloaded_state_script_content[json_start : json_end + 1].strip()
                try:
                    state_data = json.loads(json_str)
                    
                    main_category_short = state_data.get('articleDetail', {}).get('categoryShortName')
                    sub_category_short = state_data.get('articleDetail', {}).get('subCategory')

                    print(f"[DEBUG] Raw PRELOADED_STATE categoryShortName: {main_category_short}, subCategory: {sub_category_short}")

                    if main_category_short == "it":
                        if sub_category_short and sub_category_short in SUBCATEGORY_MAP:
                            genre = f"IT/{SUBCATEGORY_MAP[sub_category_short]}"
                        else:
                            genre = "IT" # IT総合の代わり
                    elif main_category_short == "sci":
                        genre = "科学" # 科学はサブカテゴリを持たないため
                    elif main_category_short and main_category_short in CATEGORY_MAP:
                        main_genre_jp = CATEGORY_MAP[main_category_short]
                        if sub_category_short and sub_category_short in SUBCATEGORY_MAP:
                            sub_genre_jp = SUBCATEGORY_MAP[sub_category_short]
                            genre = f"{main_genre_jp}/{sub_genre_jp}"
                        else:
                         # サブカテゴリが見つからない場合やマッピングにない場合は、「大カテゴリ/大カテゴリ総合」とする
                         if main_genre_jp != "主要":
                             genre = f"{main_genre_jp}/{main_genre_jp}総合"
                         else:
                             # 主要カテゴリは「主要総合」とはせず、「主要」のままにする
                             genre = "主要"
                    else:
                        cat_path = state_data.get('pageData', {}).get('pageParam', {}).get('cat_path')
                        if cat_path:
                            path_parts = cat_path.split(',')
                            if path_parts:
                                main_category_short_from_path = path_parts[0]
                                if main_category_short_from_path in CATEGORY_MAP:
                                    main_genre_jp = CATEGORY_MAP[main_category_short_from_path]
                                    if len(path_parts) > 1 and path_parts[1] in SUBCATEGORY_MAP:
                                        sub_genre_jp = SUBCATEGORY_MAP[path_parts[1]]
                                        genre = f"{main_genre_jp}/{sub_genre_jp}"
                                    else:
                                        genre = f"{main_genre_jp}総合" if main_genre_jp != "主要" else "その他"
                                else:
                                    if main_category_short_from_path in SUBCATEGORY_MAP:
                                        genre = SUBCATEGORY_MAP[main_category_short_from_path]
                                    else:
                                        genre = "その他"
                            else:
                                genre = "その他"
                        else:
                            genre = "その他"

                    print(f"[DEBUG] Extracted genre from __PRELOADED_STATE__: {genre} (main short name: {main_category_short}, sub short name: {sub_category_short})")

                except json.JSONDecodeError as e:
                    print(f"[DEBUG] Failed to parse __PRELOADED_STATE__ JSON: {e}")
                    print(f"[DEBUG] JSON string that caused error (first 200 chars): {json_str[:200]}...")
                except Exception as e:
                    print(f"[DEBUG] An unexpected error occurred while processing __PRELOADED_STATE__ (regex method): {e}")
            else:
                print("[DEBUG] Could not find balanced JSON object within __PRELOADED_STATE__ content.")
        else:
            print("[DEBUG] __PRELOADED_STATE__ script content not found in any script tag. Falling back to URL inference.")
            # Fallback to URL inference if __PRELOADED_STATE__ is not found or parsed
            found_by_url = False
            for short_name, jp_name in SUBCATEGORY_MAP.items():
                if f"/{short_name}" in url or f"ctg={short_name}" in url or f"genre={short_name}" in url:
                    if f"/categories/domestic" in url and short_name == "soci":
                        genre = "国内/社会"
                    elif f"/articles/" in url and short_name in ["poli", "soci", "peo", "mus", "mov", "game", "base", "soc", "moto", "horse", "golf", "fig", "health", "env", "art", "biz", "mkt", "stk", "ind", "oversea", "chn", "kor", "asia", "na", "ca", "eu", "mea", "sports", "enter", "it", "sci", "itpro", "korasian"]:
                        if "domestic" in url: genre_prefix = "国内/"
                        elif "world" in url: genre_prefix = "国際/"
                        elif "economy" in url: genre_prefix = "経済/"
                        elif "entertainment" in url: genre_prefix = "エンタメ/"
                        elif "sports" in url: genre_prefix = "スポーツ/"
                        elif "it" in url: genre_prefix = "IT/"
                        elif "science" in url: genre_prefix = "科学/"
                        elif "life" in url: genre_prefix = "ライフ/"
                        elif "local" in url: genre_prefix = "地域/"
                        else: genre_prefix = ""
                        
                        if short_name == "it": genre = "IT"
                        elif short_name == "sci": genre = "科学"
                        else: genre = f"{genre_prefix}{jp_name}"
                    else:
                        genre = jp_name
                    print(f"[DEBUG] Inferred genre from URL (subcategory priority): {genre}")
                    found_by_url = True
                    break
            
            if not found_by_url:
                for short_name, jp_name in CATEGORY_MAP.items():
                    if f"/categories/{short_name}" in url or f"ctg={short_name}" in url:
                        if short_name == "it": genre = "IT"
                        elif short_name == "sci": genre = "科学"
                        elif short_name == "main": genre = "主要"
                        else: genre = f"{jp_name}/{jp_name}総合"
                        print(f"[DEBUG] Inferred genre from URL (main category fallback): {genre}")
                        found_by_url = True
                        break
            
            if not found_by_url:
                genre = "その他"

        print(f"[DEBUG] Final genre before return: {genre}")
        # 6. 本文の抽出 (マルチページ対応を削除し、単一ページとして処理)
        body = extract_body(soup)

        return article_id, title, provider, pub_time, body[:3000] if body else "", genre # 本文を3000文字に制限
    except Exception as e:
        print(f"[ERROR] Failed to extract article info from {url}: {e}")
        return "ERROR", "ERROR", "ERROR", "ERROR", "", "ERROR" # エラー時には適切なデフォルト値を返す

# --- スプレッドシートへ書き込み関数 ---
def append_to_sheet(data, existing_urls):
    """
    収集したデータをGoogle Sheetsに追記します。
    既存のURLを重複して書き込まないようにフィルタリングします。
    """
    print(f"[INFO] Writing {len(data)} new records to the sheet...")
    # 認証情報を設定
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    client = gspread.authorize(creds)
    sheet = client.open(SHEET_NAME).sheet1

    # ヘッダー行が存在しない場合に挿入
    existing = sheet.get_all_values()
    if not existing:
        headers = ["ID", "収集時刻", "タイトル", "情報源", "掲載時刻", "URL", "ジャンル", "本文"]
        sheet.append_row(headers)
        print("[INFO] Header row inserted.")
        # ヘッダー追加後、再度既存データを取得して existing_urls を更新
        existing = sheet.get_all_values()
        existing_urls = {row[5] for row in existing[1:] if len(row) > 5}

    # 新規URLのレコードのみフィルタリング
    new_rows = []
    for row in data:
        # row[5] は URL
        if row[5] not in existing_urls:
            new_rows.append(row)
            existing_urls.add(row[5]) # 新しく追加されるURLを既存リストに追加しておく

    print(f"[INFO] {len(new_rows)} new unique records to write.")

    if new_rows:
        # 一括書き込み
        sheet.append_rows(new_rows, value_input_option="RAW")
        print(f"[INFO] Completed. {len(new_rows)} rows added.")
    else:
        print("[INFO] No new records to write.")


# --- メイン処理 ---
if __name__ == "__main__":
    print("[START] Yahoo News scraping started.")
    
    # このwhile Trueループとtime.sleep(3600)は削除します。
    # GitHub Actionsのcronスケジュールがこの役割を担います。
    # while True: 
    driver = None # 各実行の開始時にドライバーをNoneに初期化
    try:
        driver = init_driver()

        jst = timezone(timedelta(hours=9))
        timestamp = datetime.now(jst).strftime("%Y/%m/%d %H:%M")

        # --- カテゴリ優先度設定 ---
        # 国内および地域のカテゴリ・サブカテゴリは優先度0
        # その他すべてのカテゴリ・サブカテゴリは優先度1（同率）
        CATEGORY_PRIORITY = {
            "国内": 0,
            "国内総合": 0,
            "国内/政治": 0,
            "国内/社会": 0,
            "国内/人": 0,
            "地域": 0,
            "地域総合": 0,
            "地域/北海道・東北": 0,
            "地域/関東": 0,
            "地域/信越・北陸": 0,
            "地域/東海": 0,
            "地域/近畿": 0,
            "地域/中国": 0,
            "地域/四国": 0,
            "地域/九州・沖縄": 0,
            
            # それ以外のすべてのカテゴリとサブカテゴリは優先度1
            "主要": 1,
            "その他": 1,

            "国際": 1,
            "国際総合": 1,
            "国際/国際総合": 1,
            "国際/中国・台湾": 1,
            "国際/韓国・北朝鮮": 1,
            "国際/アジア・オセアニア": 1,
            "国際/北米": 1,
            "国際/中南米": 1,
            "国際/ヨーロッパ": 1,
            "国際/中東・アフリカ": 1,

            "経済": 1,
            "経済総合": 1,
            "経済/経済総合": 1,
            "経済/市況": 1,
            "経済/株式": 1,
            "経済/産業": 1,

            "エンタメ": 1,
            "エンタメ総合": 1,
            "エンタメ/音楽": 1,
            "エンタメ/映画": 1,
            "エンタメ/ゲーム": 1,
            "エンタメ/アジア・韓流": 1,

            "スポーツ": 1,
            "スポーツ総合": 1,
            "sports/野球": 1,
            "スポーツ/サッカー": 1,
            "スポーツ/モータースポーツ": 1,
            "スポーツ/競馬": 1,
            "スポーツ/ゴルフ": 1,
            "スポーツ/格闘技": 1,

            "IT": 1,
            "IT総合": 1,
            "IT/製品": 1,

            "科学": 1,
            "科学総合": 1,
            
            "ライフ": 1,
            "ライフ総合": 1,
            "ライフ/ヘルス": 1,
            "ライフ/環境": 1,
            "ライフ/文化・アート": 1,
        }


        category_urls = {
            "国内": "https://news.yahoo.co.jp/categories/domestic",
            "国際": "https://news.yahoo.co.jp/categories/world",
            "経済": "https://news.yahoo.co.jp/categories/economy",
            "エンタメ": "https://news.yahoo.co.jp/categories/entertainment",
            "スポーツ": "https://news.yahoo.co.jp/categories/sports",
            "IT": "https://news.yahoo.co.jp/categories/it",
            "科学": "https://news.yahoo.co.jp/categories/science",
            "ライフ": "https://news.yahoo.co.jp/categories/life",
            "地域": "https://news.yahoo.co.jp/categories/local",
            "主要": "https://news.yahoo.co.jp/"
        }

        # すべての記事を一時的に保持する辞書。key: URL, value: [article_id, collected_at, title, provider, pub_time, url, genre, body]
        # 同じURLでより優先度の高いジャンルが見つかった場合、この辞書のエントリを更新する
        temp_article_storage = {}
        
        # 既存URLのセットをスプレッドシートから読み込む
        try:
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
            client = gspread.authorize(creds)
            sheet = client.open(SHEET_NAME).sheet1
            existing_urls_on_sheet = {row[5] for row in sheet.get_all_values()[1:] if len(row) > 5}
            print(f"[INFO] Fetched {len(existing_urls_on_sheet)} existing URLs from the sheet.")
        except Exception as e:
            print(f"[ERROR] Could not connect to Google Sheet or fetch existing URLs: {e}")
            existing_urls_on_sheet = set() # エラー時は空のセットで続行

        total_skipped = 0
        total_added = 0
        total_updated_genre = 0 # ジャンルが更新された記事の数

        for category_name, base_url in category_urls.items():
            print(f"\n--- Scraping Category: {category_name} ({base_url}) ---")
            driver.get(base_url)
            time.sleep(3) # 各カテゴリページ読み込み時の待機

            # 各カテゴリで「もっと見る」のクリック回数を減らす
            # アカウント停止リスクを減らすため、1回または0回にするのも有効
            # Yahoo!ニュースは新しい記事が比較的頻繁に出るので、少なめでも良いかも
            for i in range(1): # 例: 1回だけクリック (デフォルトは3回から変更)
                try:
                    more_button = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, "//button[contains(text(),'もっと見る')]"))
                    )
                    driver.execute_script("arguments[0].click();", more_button)
                    print(f"[DEBUG] Clicked 'もっと見る' button {i+1} times in {category_name}.")
                    time.sleep(2) # 新しい記事が読み込まれるのを待つ
                except Exception:
                    print(f"[INFO] No more 'もっと見る' button or reached limit in {category_name}.")
                    break
            
            soup = BeautifulSoup(driver.page_source, "html.parser")
            article_links = soup.select("a[href^='https://news.yahoo.co.jp/articles/']")
            print(f"[DEBUG] Found {len(article_links)} article links in {category_name}.")
            
            for a in article_links:
                article_url = a["href"].split("?")[0]
                
                # 既存スプレッドシートにある場合はスキップ（初回追加はしない）
                if article_url in existing_urls_on_sheet:
                    total_skipped += 1
                    continue
                
                # 記事情報の抽出
                article_id, title, provider, pub_time, body, genre = extract_article_info(driver, article_url)

                if title == "ERROR" or not body:
                    print(f"[SKIP] Invalid content or error occurred for: {article_url}")
                    total_skipped += 1
                    continue

                current_article_data = [
                    article_id,
                    timestamp,
                    title,
                    provider,
                    pub_time,
                    article_url,
                    genre, # ここで取得されたジャンル
                    body
                ]

                # 収集済みリスト（temp_article_storage）に既に存在するか確認
                if article_url in temp_article_storage:
                    existing_genre = temp_article_storage[article_url][6] # 既存のジャンル
                    
                    # ジャンルを特定できない場合のデフォルト値を考慮
                    existing_priority = CATEGORY_PRIORITY.get(existing_genre, 0) # 辞書にない場合は0（最低）
                    new_priority = CATEGORY_PRIORITY.get(genre, 0) # 辞書にない場合は0（最低）

                    # 新しいジャンルが既存のジャンルよりも優先度が高い場合、または同じ優先度でより詳細な場合
                    if new_priority > existing_priority:
                        temp_article_storage[article_url] = current_article_data
                        total_updated_genre += 1
                        print(f"[UPDATE] Genre updated for {article_id}: From '{existing_genre}' (Priority {existing_priority}) to '{genre}' (Priority {new_priority})")
                    elif new_priority == existing_priority and len(genre) > len(existing_genre):
                        # 同じ優先度でも、より詳細なジャンル名（例: 国内総合 -> 国内/社会）を優先
                        temp_article_storage[article_url] = current_article_data
                        total_updated_genre += 1
                        print(f"[UPDATE] Genre updated for {article_id}: From '{existing_genre}' to '{genre}' (more detailed)")
                else:
                    # 初めて見つかった記事
                    temp_article_storage[article_url] = current_article_data
                    total_added += 1
                    print(f"[ADD] {title} (ID: {article_id}) - Genre: {genre}")
        
        # この try-except ブロックの最後で driver を終了させます。
        # 各カテゴリのスクレイピングが終わるたびにdriver.quit()を呼び出すのは効率が悪いので、
        # 全てのカテゴリを回った後、メインループの最後に移します。
        # driver.quit() は最後に一回だけ実行するのが一般的です。

    except Exception as main_e:
        print(f"[CRITICAL ERROR] An error occurred during the scraping process: {main_e}")
    finally: # エラーの有無にかかわらず、最後にドライバーを終了させる
        if driver:
            driver.quit()
            print("[INFO] WebDriver closed.")

    # temp_article_storageから、新規追加すべきレコードを抽出
    # スプレッドシートに書き込む前に、再度重複チェックを行う（念のため）
    final_data_to_write = []
    for url, data in temp_article_storage.items():
        if url not in existing_urls_on_sheet:
            final_data_to_write.append(data)

    if final_data_to_write:
        append_to_sheet(final_data_to_write, existing_urls_on_sheet)
        # スプレッドシートに書き込んだら、既存URLリストを更新
        # 次の実行時には、この新しいURLもスキップ対象になる
        existing_urls_on_sheet.update({row[5] for row in final_data_to_write}) 
    else:
        print("[INFO] No new unique articles to write across all categories after priority sorting.")

    print(f"[REPORT] Total Skipped (already in sheet): {total_skipped}")
    print(f"[REPORT] Total Added (new unique articles): {total_added}")
    print(f"[REPORT] Total Genre Updated (higher priority): {total_updated_genre}")
    print("[END] Yahoo News scraping finished this cycle.")

    # GitHub Actionsのcronスケジュールが次の実行をトリガーするため、
    # Pythonスクリプト自体がスリープする必要はありません。
    # このスクリプトは1回の実行で完結します。
    # print(f"\nSleeping for 3600 seconds until the next run at {datetime.now(jst) + timedelta(seconds=3600)} JST...")
    # time.sleep(3600)
