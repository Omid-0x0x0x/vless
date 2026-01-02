#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simple Config Processor - دانلود، دسته‌بندی و Split بدون پینگ
"""

import urllib.request
import base64
import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Set
from collections import defaultdict

class SimpleProcessor:
    def __init__(self, split_size: int = 300):
        self.split_size = split_size
        self.configs: Set[str] = set()  # برای حذف تکراری
        
    def is_base64(self, text: str) -> bool:
        """چک کردن اینکه متن base64 هست یا نه"""
        text = text.strip()
        if text.startswith(('vless://', 'vmess://')):
            return False
        pattern = re.compile(r'^[A-Za-z0-9+/]*={0,2}$')
        return len(text) > 20 and pattern.match(text)
    
    def decode_base64(self, content: bytes) -> str:
        """تبدیل محتوا از base64 اگه لازم باشه"""
        try:
            text = content.decode('utf-8', errors='ignore').strip()
            
            # تا ۵ لایه base64 رو decode می‌کنه
            for _ in range(5):
                if self.is_base64(text):
                    try:
                        missing_padding = len(text) % 4
                        if missing_padding:
                            text += '=' * (4 - missing_padding)
                        decoded = base64.b64decode(text).decode('utf-8', errors='ignore').strip()
                        if decoded != text:
                            text = decoded
                        else:
                            break
                    except:
                        break
                else:
                    break
            
            return text
        except:
            return content.decode('utf-8', errors='ignore')
    
    def fetch_url(self, url: str) -> List[str]:
        """دانلود کانفیگ‌ها از یک URL"""
        try:
            print(f"📥 Downloading: {url}")
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                content = response.read()
                decoded = self.decode_base64(content)
                configs = [line.strip() for line in decoded.split('\n') if line.strip()]
                
                # فقط کانفیگ‌های VLESS رو نگه می‌داره
                vless_configs = [c for c in configs if c.startswith('vless://')]
                print(f"   ✓ Found {len(vless_configs)} VLESS configs")
                return vless_configs
        except Exception as e:
            print(f"   ✗ Error: {e}")
            return []
    
    def fetch_all(self, urls: List[str]) -> List[str]:
        """دانلود موازی از همه URL ها"""
        print("\n" + "="*60)
        print("📥 Fetching configs from all URLs...")
        print("="*60)
        
        all_configs = []
        
        # دانلود موازی با ۱۰ thread
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(self.fetch_url, url): url for url in urls}
            
            for future in as_completed(futures):
                configs = future.result()
                all_configs.extend(configs)
        
        print(f"\n✓ Total downloaded: {len(all_configs)} configs")
        return all_configs
    
    def remove_duplicates(self, configs: List[str]) -> List[str]:
        """حذف کانفیگ‌های تکراری"""
        print("\n🔄 Removing duplicates...")
        unique = list(set(configs))
        print(f"✓ Unique configs: {len(unique)} (removed {len(configs) - len(unique)} duplicates)")
        return unique
    
    def extract_transport_type(self, config: str) -> str:
        """تشخیص نوع transport از کانفیگ"""
        try:
            # پیدا کردن query string
            if '?' not in config:
                return 'tcp'
            
            query_string = config.split('?')[1].split('#')[0]
            params = {}
            
            for param in query_string.split('&'):
                if '=' in param:
                    key, value = param.split('=', 1)
                    params[key] = value
            
            # چک کردن type
            transport = params.get('type', 'tcp')
            
            # چک کردن security برای TLS
            security = params.get('security', 'none')
            
            # اولویت‌بندی
            if transport == 'ws':
                return 'ws'
            elif transport == 'grpc':
                return 'grpc'
            elif transport == 'httpupgrade' or transport == 'xhttp':
                return 'xhttp'
            elif security == 'tls':
                return 'tls'
            else:
                return 'tcp'
                
        except:
            return 'tcp'
    
    def categorize_by_transport(self, configs: List[str]) -> Dict[str, List[str]]:
        """دسته‌بندی کانفیگ‌ها بر اساس transport"""
        print("\n📊 Categorizing by transport type...")
        
        categories = defaultdict(list)
        
        for config in configs:
            transport = self.extract_transport_type(config)
            categories[transport].append(config)
        
        # نمایش آمار
        for transport, confs in categories.items():
            print(f"   {transport.upper()}: {len(confs)} configs")
        
        return dict(categories)
    
    def save_all_configs(self, configs: List[str], output_dir: str):
        """ذخیره تمام کانفیگ‌ها در یک فایل"""
        filepath = os.path.join(output_dir, 'all_vless_config.txt')
        with open(filepath, 'w', encoding='utf-8') as f:
            for config in configs:
                f.write(config + '\n')
        print(f"\n✓ Saved all configs to: all_vless_config.txt")
    
    def save_by_transport(self, categories: Dict[str, List[str]], output_dir: str):
        """ذخیره کانفیگ‌ها به تفکیک transport"""
        print("\n💾 Saving categorized configs...")
        
        for transport, configs in categories.items():
            filepath = os.path.join(output_dir, f'vless_{transport}.txt')
            with open(filepath, 'w', encoding='utf-8') as f:
                for config in configs:
                    f.write(config + '\n')
            print(f"   ✓ vless_{transport}.txt ({len(configs)} configs)")
    
    def split_configs(self, configs: List[str], output_dir: str):
        """Split کانفیگ‌ها به فایل‌های ۳۰۰ تایی"""
        print(f"\n✂️  Splitting into {self.split_size}-config files...")
        
        total_files = (len(configs) + self.split_size - 1) // self.split_size
        
        for i in range(0, len(configs), self.split_size):
            chunk = configs[i:i + self.split_size]
            file_num = (i // self.split_size) + 1
            filepath = os.path.join(output_dir, f'vless_config_{file_num}.txt')
            
            with open(filepath, 'w', encoding='utf-8') as f:
                for config in chunk:
                    f.write(config + '\n')
        
        print(f"   ✓ Created {total_files} split files")
    
    def update_readme(self, output_dir: str, repo_url: str):
        """به روز کردن README با لینک‌های raw"""
        print("\n📝 Updating README.md...")
        
        # پیدا کردن تمام فایل‌های txt
        files = [f for f in os.listdir(output_dir) if f.endswith('.txt')]
        files.sort()
        
        # ساخت محتوای README
        readme_content = f"""# 🚀 VLESS Configs Repository

Auto-updated every 6 hours with fresh VLESS configurations.

## 📊 Statistics

- **Total Files**: {len(files)}
- **Last Update**: Auto-generated
- **Update Frequency**: Every 6 hours

## 📁 Available Files

### All Configs
"""
        
        # اضافه کردن لینک‌های raw
        for file in files:
            # ساخت URL raw
            raw_url = f"{repo_url}/raw/main/configs/{file}"
            readme_content += f"\n- [{file}]({raw_url})"
        
        readme_content += """

## 🔗 How to Use

Copy any raw link above and add it as a subscription in your V2Ray client.

### Example:
```
https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/configs/vless_config_1.txt
```

## ⚙️ Transport Types

Configs are categorized by transport protocol:
- **WS**: WebSocket
- **gRPC**: Google RPC
- **TCP**: Standard TCP
- **TLS**: With TLS encryption
- **XHTTP**: HTTP Upgrade

---

*Auto-updated by GitHub Actions*
"""
        
        # ذخیره README
        readme_path = os.path.join(os.path.dirname(output_dir), 'README.md')
        with open(readme_path, 'w', encoding='utf-8') as f:
            f.write(readme_content)
        
        print("   ✓ README.md updated with raw links")

def main():
    import sys
    
    # خواندن آرگومان‌ها
    if len(sys.argv) < 2:
        print("Usage: python3 simple_processor.py <subscriptions_file>")
        sys.exit(1)
    
    subs_file = sys.argv[1]
    
    # خواندن لیست subscription ها
    if not os.path.exists(subs_file):
        print(f"✗ File not found: {subs_file}")
        sys.exit(1)
    
    with open(subs_file, 'r') as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    
    print(f"📋 Found {len(urls)} subscription URLs")
    
    # ساخت پوشه output
    output_dir = 'configs'
    os.makedirs(output_dir, exist_ok=True)
    
    # پردازش
    processor = SimpleProcessor(split_size=300)
    
    # دانلود
    all_configs = processor.fetch_all(urls)
    
    if not all_configs:
        print("\n✗ No configs downloaded!")
        sys.exit(1)
    
    # حذف تکراری
    unique_configs = processor.remove_duplicates(all_configs)
    
    # دسته‌بندی
    categories = processor.categorize_by_transport(unique_configs)
    
    # ذخیره فایل‌ها
    processor.save_all_configs(unique_configs, output_dir)
    processor.save_by_transport(categories, output_dir)
    processor.split_configs(unique_configs, output_dir)
    
    # به روز کردن README
    # شما باید URL repository خودتون رو اینجا بذارید
    repo_url = "https://github.com/Matt-Ranaei/vless"  # این رو تغییر بدید
    processor.update_readme(output_dir, repo_url)
    
    print("\n" + "="*60)
    print("✅ All done!")
    print("="*60)
    print(f"📁 Output directory: {output_dir}/")
    print(f"📊 Total unique configs: {len(unique_configs)}")
    print(f"📝 README.md updated with raw links")
    print("="*60)

if __name__ == '__main__':
    main()
