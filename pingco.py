#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PingCo - VLESS/VMESS Config Ping Tester & Manager
Multi-threaded config fetcher, ICMP pinger, and Telegram notifier
"""

import argparse
import base64
import sys
import os
import re
import time
import subprocess
import json
import socket
import tempfile
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from typing import List, Dict, Set, Tuple, Optional
import urllib.request
import urllib.error
from threading import Lock
import gc

# Try to import for interactive menu
try:
    import curses
    HAS_CURSES = True
except ImportError:
    HAS_CURSES = False

# Try to import socks for proxy support
try:
    import socks
    HAS_SOCKS = True
except ImportError:
    HAS_SOCKS = False

# Progress bar
class ProgressBar:
    def __init__(self, total: int, desc: str = "Processing"):
        self.total = total
        self.current = 0
        self.desc = desc
        self.lock = Lock()
        
    def update(self, n: int = 1):
        with self.lock:
            self.current += n
            self._display()
    
    def _display(self):
        percent = (self.current / self.total) * 100 if self.total > 0 else 0
        bar_length = 40
        filled = int(bar_length * self.current / self.total) if self.total > 0 else 0
        bar = '█' * filled + '░' * (bar_length - filled)
        sys.stdout.write(f'\r{self.desc}: [{bar}] {percent:.1f}% [{self.current}/{self.total}]')
        sys.stdout.flush()
        if self.current >= self.total:
            print()

# Config utilities
class ConfigUtils:
    @staticmethod
    def extract_ip_from_config(config: str) -> Optional[str]:
        """Extract IP/domain from VLESS/VMESS config"""
        try:
            if config.startswith('vless://'):
                # vless://uuid@ip:port?params
                match = re.search(r'@([^:]+):', config)
                if match:
                    return match.group(1)
            elif config.startswith('vmess://'):
                # vmess is base64 encoded JSON
                config_data = config.replace('vmess://', '')
                decoded = base64.b64decode(config_data + '==').decode('utf-8')
                match = re.search(r'"add"\s*:\s*"([^"]+)"', decoded)
                if match:
                    return match.group(1)
        except Exception:
            pass
        return None
    
    @staticmethod
    def is_valid_config(config: str) -> bool:
        """Check if config is valid VLESS/VMESS"""
        return config.startswith(('vless://', 'vmess://'))
    
    @staticmethod
    def normalize_config(config: str) -> str:
        """Normalize config string"""
        return config.strip()

# ICMP Ping
class ICMPPing:
    @staticmethod
    def ping(host: str, timeout: int = 2, count: int = 1) -> Optional[float]:
        """
        Ping a host and return average latency in ms
        Returns None if host is unreachable
        """
        try:
            # Platform-specific ping command
            if sys.platform.startswith('win'):
                cmd = ['ping', '-n', str(count), '-w', str(timeout * 1000), host]
            else:
                cmd = ['ping', '-c', str(count), '-W', str(timeout), host]
            
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout + 1,
                text=True
            )
            
            if result.returncode == 0:
                # Parse ping output
                output = result.stdout
                if sys.platform.startswith('win'):
                    match = re.search(r'Average = (\d+)ms', output)
                else:
                    match = re.search(r'avg[^=]*=\s*[\d.]+/([\d.]+)/', output)
                
                if match:
                    return float(match.group(1))
            return None
        except (subprocess.TimeoutExpired, Exception):
            return None

# Real Ping (through proxy)
class RealPing:
    def __init__(self, test_hosts: List[str] = None):
        self.test_hosts = test_hosts or ['1.1.1.1']
    
    def test_config(self, config: str, timeout: int = 8) -> Optional[float]:
        """Test config by connecting and pinging real hosts"""
        import signal
        
        def timeout_handler(signum, frame):
            raise TimeoutError("Config test timeout")
        
        proxy_manager = V2RayProxyManager()
        original_socket = None
        
        try:
            # Set timeout for entire operation
            if sys.platform != 'win32':
                signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(timeout)
            
            # Try to connect
            if not proxy_manager.start_proxy(config):
                proxy_manager.stop_proxy()
                if sys.platform != 'win32':
                    signal.alarm(0)
                return None
            
            # Test with ping through proxy
            if not HAS_SOCKS:
                proxy_manager.stop_proxy()
                if sys.platform != 'win32':
                    signal.alarm(0)
                return None
            
            import socks
            import socket as sock_module
            
            # Save original
            original_socket = sock_module.socket
            
            # Set proxy
            socks.set_default_proxy(socks.SOCKS5, "127.0.0.1", proxy_manager.socks_port)
            sock_module.socket = socks.socksocket
            
            # Test connectivity (quick test)
            start = time.time()
            try:
                req = urllib.request.Request('http://1.1.1.1', headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=3) as response:
                    elapsed = (time.time() - start) * 1000
            except:
                elapsed = None
            
            # Restore
            sock_module.socket = original_socket
            proxy_manager.stop_proxy()
            
            # Cancel alarm
            if sys.platform != 'win32':
                signal.alarm(0)
            
            return elapsed
            
        except (TimeoutError, Exception):
            if original_socket:
                try:
                    import socket as sock_module
                    sock_module.socket = original_socket
                except:
                    pass
            proxy_manager.stop_proxy()
            if sys.platform != 'win32':
                try:
                    signal.alarm(0)
                except:
                    pass
            return None

# Config Fetcher
class ConfigFetcher:
    def __init__(self, max_workers: int = 10):
        self.max_workers = max_workers
    
    @staticmethod
    def is_base64(text: str) -> bool:
        """Check if text is likely base64 encoded"""
        # Remove whitespace
        text = text.strip()
        
        # Base64 characteristics:
        # - Length multiple of 4 (after adding padding)
        # - Only contains base64 chars
        # - Doesn't look like a config already
        if text.startswith(('vless://', 'vmess://', 'ss://', 'trojan://', 'http://', 'https://')):
            return False
        
        # Check for base64 characters (A-Z, a-z, 0-9, +, /, =)
        base64_pattern = re.compile(r'^[A-Za-z0-9+/]*={0,2}$')
        
        # Should be reasonable length and match pattern
        return len(text) > 20 and base64_pattern.match(text)
    
    @staticmethod
    def smart_decode(content: bytes) -> str:
        """
        Smart decoder that handles:
        - Plain text
        - Base64 encoded text
        - Nested base64 (base64 inside base64)
        - Mixed content
        """
        try:
            # First try to decode as UTF-8
            text = content.decode('utf-8', errors='ignore').strip()
            
            # Try to decode if it looks like base64
            max_iterations = 5  # Prevent infinite loops
            iteration = 0
            
            while iteration < max_iterations:
                # Check if entire content is base64
                if ConfigFetcher.is_base64(text):
                    try:
                        # Try to decode
                        # Add padding if needed
                        missing_padding = len(text) % 4
                        if missing_padding:
                            text += '=' * (4 - missing_padding)
                        
                        decoded = base64.b64decode(text).decode('utf-8', errors='ignore').strip()
                        
                        # If decoded successfully and got different result, continue
                        if decoded and decoded != text:
                            text = decoded
                            iteration += 1
                        else:
                            break
                    except Exception:
                        break
                else:
                    # Not base64, we're done
                    break
            
            return text
            
        except Exception:
            # Fallback to plain decode
            try:
                return content.decode('utf-8', errors='ignore')
            except:
                return content.decode('latin-1', errors='ignore')
    
    def fetch_url(self, url: str) -> Tuple[str, Optional[str]]:
        """Fetch configs from URL with smart base64 decoding"""
        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                content = response.read()
                decoded = self.smart_decode(content)
                return url, decoded
        except Exception as e:
            return url, None
    
    def fetch_multiple(self, urls: List[str]) -> List[str]:
        """Fetch configs from multiple URLs with progress"""
        all_configs = []
        progress = ProgressBar(len(urls), "Fetching URLs")
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.fetch_url, url): url for url in urls}
            
            for future in as_completed(futures):
                url, content = future.result()
                progress.update(1)
                
                if content:
                    configs = [line.strip() for line in content.split('\n') if line.strip()]
                    all_configs.extend(configs)
        
        return all_configs

# Config Processor
class ConfigProcessor:
    def __init__(self, max_workers: int = 50):
        self.max_workers = max_workers
        self.configs_set: Set[str] = set()
        self.lock = Lock()
    
    def remove_duplicates(self, configs: List[str]) -> List[str]:
        """Remove duplicate configs efficiently"""
        seen = set()
        unique = []
        for config in configs:
            normalized = ConfigUtils.normalize_config(config)
            if normalized and ConfigUtils.is_valid_config(normalized):
                if normalized not in seen:
                    seen.add(normalized)
                    unique.append(normalized)
        return unique
    
    def ping_config(self, config: str) -> Tuple[str, Optional[float]]:
        """Ping a single config and return (config, latency)"""
        ip = ConfigUtils.extract_ip_from_config(config)
        if not ip:
            return config, None
        
        latency = ICMPPing.ping(ip, timeout=2, count=1)
        return config, latency
    
    def real_ping_config(self, config: str, real_pinger: RealPing) -> Tuple[str, Optional[float]]:
        """Real ping through proxy"""
        latency = real_pinger.test_config(config, timeout=8)
        return config, latency
    
    def ping_configs(self, configs: List[str]) -> List[Tuple[str, float]]:
        """Ping multiple configs with progress"""
        results = []
        progress = ProgressBar(len(configs), "Pinging configs")
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.ping_config, config): config for config in configs}
            
            for future in as_completed(futures):
                config, latency = future.result()
                progress.update(1)
                
                if latency is not None:
                    results.append((config, latency))
                
                # Clear memory periodically
                if len(results) % 1000 == 0:
                    gc.collect()
        
        return results
    
    def real_ping_configs(self, configs: List[str]) -> List[Tuple[str, float]]:
        """Real ping multiple configs (sequential for stability)"""
        results = []
        real_pinger = RealPing()
        progress = ProgressBar(len(configs), "Real ping test")
        
        for config in configs:
            latency = real_pinger.test_config(config, timeout=8)
            progress.update(1)
            
            if latency is not None:
                results.append((config, latency))
            
            if len(results) % 50 == 0:
                gc.collect()
        
        return results
    
    def sort_by_latency(self, results: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
        """Sort configs by latency using efficient algorithm"""
        # Timsort (Python's built-in) is O(n log n) which is optimal for comparison sorts
        # It's already optimized for real-world data
        return sorted(results, key=lambda x: x[1])

# File Manager
class FileManager:
    @staticmethod
    def read_file(filepath: str) -> List[str]:
        """Read configs from file with smart base64 decoding"""
        try:
            with open(filepath, 'rb') as f:
                content = f.read()
            
            # Use smart decoder
            decoded_content = ConfigFetcher.smart_decode(content)
            
            # Split by lines and clean up
            lines = [line.strip() for line in decoded_content.split('\n') if line.strip()]
            
            return lines
        except Exception as e:
            print(f"Error reading file {filepath}: {e}")
            return []
    
    @staticmethod
    def write_configs(filepath: str, configs: List[str]):
        """Write configs to file"""
        try:
            os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
            with open(filepath, 'w', encoding='utf-8') as f:
                for config in configs:
                    f.write(config + '\n')
        except Exception as e:
            print(f"Error writing file {filepath}: {e}")
    
    @staticmethod
    def write_results(filepath: str, results: List[Tuple[str, float]]):
        """Write ping results to file"""
        try:
            os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
            with open(filepath, 'w', encoding='utf-8') as f:
                for config, latency in results:
                    f.write(f"{config}\n")
        except Exception as e:
            print(f"Error writing file {filepath}: {e}")
    
    @staticmethod
    def split_and_save(configs: List[str], base_name: str, split_size: int):
        """Split configs into multiple files with progress"""
        total_files = (len(configs) + split_size - 1) // split_size
        progress = ProgressBar(total_files, "Saving split files")
        
        for i in range(0, len(configs), split_size):
            chunk = configs[i:i + split_size]
            file_num = (i // split_size) + 1
            filename = f"{base_name}{file_num}.txt"
            FileManager.write_configs(filename, chunk)
            progress.update(1)
        
        print(f"✓ Split into {total_files} files ({split_size} configs each)")

# V2Ray/Xray Proxy Manager
class V2RayProxyManager:
    def __init__(self, socks_port: int = 10808):
        self.socks_port = socks_port
        self.process = None
        self.config_file = None
        self.xray_path = self._find_xray()
    
    def _find_xray(self) -> Optional[str]:
        """Find xray-core or v2ray-core executable"""
        # Common paths
        paths = [
            'xray',
            '/usr/local/bin/xray',
            '/usr/bin/xray',
            'v2ray',
            '/usr/local/bin/v2ray',
            '/usr/bin/v2ray',
        ]
        
        for path in paths:
            try:
                result = subprocess.run(
                    [path, 'version'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=2
                )
                if result.returncode == 0:
                    return path
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        
        return None
    
    def _parse_vless_config(self, config: str) -> Optional[Dict]:
        """Parse VLESS config to JSON"""
        try:
            # vless://uuid@address:port?params#remark
            match = re.match(r'vless://([^@]+)@([^:]+):(\d+)\??(.*)#?(.*)', config)
            if not match:
                return None
            
            uuid, address, port, params, remark = match.groups()
            
            # Parse parameters
            param_dict = {}
            if params:
                for param in params.split('&'):
                    if '=' in param:
                        key, value = param.split('=', 1)
                        param_dict[key] = urllib.parse.unquote(value)
            
            # Build outbound config
            outbound = {
                "protocol": "vless",
                "settings": {
                    "vnext": [{
                        "address": address,
                        "port": int(port),
                        "users": [{
                            "id": uuid,
                            "encryption": param_dict.get('encryption', 'none')
                        }]
                    }]
                },
                "streamSettings": {
                    "network": param_dict.get('type', 'tcp'),
                    "security": param_dict.get('security', 'none')
                }
            }
            
            # Add TLS settings
            if param_dict.get('security') == 'tls':
                outbound['streamSettings']['tlsSettings'] = {
                    "serverName": param_dict.get('sni', address)
                }
            
            # Add transport settings
            if param_dict.get('type') == 'ws':
                outbound['streamSettings']['wsSettings'] = {
                    "path": param_dict.get('path', '/'),
                    "headers": {
                        "Host": param_dict.get('host', address)
                    }
                }
            elif param_dict.get('type') == 'grpc':
                outbound['streamSettings']['grpcSettings'] = {
                    "serviceName": param_dict.get('serviceName', '')
                }
            
            return outbound
            
        except Exception as e:
            return None
    
    def _parse_vmess_config(self, config: str) -> Optional[Dict]:
        """Parse VMESS config to JSON"""
        try:
            # vmess://base64encoded
            config_data = config.replace('vmess://', '')
            decoded = base64.b64decode(config_data + '==').decode('utf-8')
            vmess_data = json.loads(decoded)
            
            outbound = {
                "protocol": "vmess",
                "settings": {
                    "vnext": [{
                        "address": vmess_data.get('add'),
                        "port": int(vmess_data.get('port')),
                        "users": [{
                            "id": vmess_data.get('id'),
                            "alterId": int(vmess_data.get('aid', 0)),
                            "security": vmess_data.get('scy', 'auto')
                        }]
                    }]
                },
                "streamSettings": {
                    "network": vmess_data.get('net', 'tcp'),
                    "security": vmess_data.get('tls', 'none')
                }
            }
            
            # Add TLS settings
            if vmess_data.get('tls') == 'tls':
                outbound['streamSettings']['tlsSettings'] = {
                    "serverName": vmess_data.get('sni', vmess_data.get('add'))
                }
            
            # Add transport settings
            if vmess_data.get('net') == 'ws':
                outbound['streamSettings']['wsSettings'] = {
                    "path": vmess_data.get('path', '/'),
                    "headers": {
                        "Host": vmess_data.get('host', vmess_data.get('add'))
                    }
                }
            
            return outbound
            
        except Exception as e:
            return None
    
    def _create_xray_config(self, outbound: Dict) -> Dict:
        """Create complete Xray config"""
        return {
            "log": {
                "loglevel": "warning"
            },
            "inbounds": [{
                "port": self.socks_port,
                "protocol": "socks",
                "settings": {
                    "auth": "noauth",
                    "udp": True
                }
            }],
            "outbounds": [outbound]
        }
    
    def start_proxy(self, config: str) -> bool:
        """Start V2Ray/Xray proxy with config"""
        if not self.xray_path:
            return False
        
        # Parse config
        outbound = None
        if config.startswith('vless://'):
            outbound = self._parse_vless_config(config)
        elif config.startswith('vmess://'):
            outbound = self._parse_vmess_config(config)
        
        if not outbound:
            return False
        
        # Create config file
        xray_config = self._create_xray_config(outbound)
        
        try:
            # Create temp config file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                json.dump(xray_config, f, indent=2)
                self.config_file = f.name
            
            # Start xray
            self.process = subprocess.Popen(
                [self.xray_path, 'run', '-c', self.config_file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
            # Wait for startup (reduced time)
            time.sleep(1)
            
            # Test connection
            if self._test_proxy():
                return True
            else:
                self.stop_proxy()
                return False
                
        except Exception as e:
            self.stop_proxy()
            return False
    
    def _test_proxy(self) -> bool:
        """Test if proxy is working"""
        try:
            # Quick port check
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex(('127.0.0.1', self.socks_port)) == 0
            sock.close()
            return result
        except:
            return False
    
    def stop_proxy(self):
        """Stop V2Ray/Xray proxy"""
        if self.process:
            try:
                self.process.kill()
                self.process.wait(timeout=1)
            except:
                pass
            self.process = None
        
        if self.config_file and os.path.exists(self.config_file):
            try:
                os.remove(self.config_file)
            except:
                pass
            self.config_file = None
    
    def __del__(self):
        self.stop_proxy()

# Config Connection Manager
class ConfigConnectionManager:
    def __init__(self, configs_with_ping: List[Tuple[str, float]]):
        self.configs = configs_with_ping
        self.proxy_manager = V2RayProxyManager()
        self.connected_config = None
    
    def try_connect_with_configs(self, max_attempts: int = 10) -> Optional[Dict]:
        """Try to connect using top configs"""
        print("\n" + "="*80)
        print("🔌 Attempting to connect via configs...")
        print("="*80)
        
        attempts = min(max_attempts, len(self.configs))
        
        for i, (config, latency) in enumerate(self.configs[:attempts], 1):
            ip = ConfigUtils.extract_ip_from_config(config)
            print(f"\n[{i}/{attempts}] Trying config: {ip} (ping: {latency:.1f}ms)")
            print(f"Config: {config[:80]}...")
            
            if self.proxy_manager.start_proxy(config):
                print(f"✓ Connected successfully!")
                self.connected_config = config
                return {
                    'config': config,
                    'latency': latency,
                    'proxy': {'host': '127.0.0.1', 'port': self.proxy_manager.socks_port}
                }
            else:
                print(f"✗ Failed to connect")
        
        return None
    
    def wait_for_connection(self) -> Optional[Dict]:
        """Keep trying to connect"""
        print("\n⏳ Waiting for connection...")
        print("Press Ctrl+C to cancel")
        
        attempt = 0
        while True:
            attempt += 1
            print(f"\n[Attempt {attempt}] Trying all configs...")
            
            result = self.try_connect_with_configs(len(self.configs))
            if result:
                return result
            
            print("⏳ Waiting 10 seconds before retry...")
            try:
                time.sleep(10)
            except KeyboardInterrupt:
                return None
    
    def disconnect(self):
        """Disconnect proxy"""
        self.proxy_manager.stop_proxy()
        self.connected_config = None
class TelegramBot:
    def __init__(self, bot_token: str, chat_id: str, proxy: Optional[Dict] = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.proxy = proxy
        self.api_url = f"https://api.telegram.org/bot{bot_token}"
    
    @staticmethod
    def detect_system_proxy() -> Optional[Dict]:
        """Detect system SOCKS5 proxy settings"""
        # Check environment variables first
        for var in ['socks_proxy', 'SOCKS_PROXY', 'all_proxy', 'ALL_PROXY']:
            proxy_url = os.environ.get(var)
            if proxy_url and 'socks' in proxy_url.lower():
                # Parse socks5://127.0.0.1:10808
                match = re.search(r'socks5?://([^:]+):(\d+)', proxy_url)
                if match:
                    return {'type': 'socks5', 'host': match.group(1), 'port': int(match.group(2))}
                return {'type': 'socks5', 'url': proxy_url}
        
        # macOS: Try to read from system preferences
        if sys.platform == 'darwin':
            try:
                # Check using scutil
                result = subprocess.run(
                    ['scutil', '--proxy'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if result.returncode == 0:
                    output = result.stdout
                    # Look for SOCKSEnable and SOCKSProxy
                    if 'SOCKSEnable : 1' in output:
                        host_match = re.search(r'SOCKSProxy : (.+)', output)
                        port_match = re.search(r'SOCKSPort : (\d+)', output)
                        if host_match and port_match:
                            return {
                                'type': 'socks5',
                                'host': host_match.group(1).strip(),
                                'port': int(port_match.group(1))
                            }
            except:
                pass
        
        # Check common proxy ports by testing connection
        for port in [10808, 1080, 9050, 1081]:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                result = sock.connect_ex(('127.0.0.1', port))
                sock.close()
                if result == 0:
                    return {'type': 'socks5', 'host': '127.0.0.1', 'port': port}
            except:
                pass
        
        return None
    
    def _make_request(self, url: str, data: bytes) -> bool:
        """Make HTTP request with optional proxy support"""
        original_socket = None
        
        try:
            # Setup proxy if needed
            if self.proxy and HAS_SOCKS:
                import socks
                import socket as sock_module
                
                # Save original socket
                original_socket = sock_module.socket
                
                # Configure SOCKS proxy
                socks.set_default_proxy(
                    socks.SOCKS5,
                    self.proxy.get('host', '127.0.0.1'),
                    self.proxy.get('port', 1080)
                )
                sock_module.socket = socks.socksocket
            elif self.proxy and not HAS_SOCKS:
                print("⚠ PySocks not installed. Install with: pip3 install PySocks --break-system-packages")
                return False
            
            # Make request
            req = urllib.request.Request(
                url,
                data=data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
            )
            
            with urllib.request.urlopen(req, timeout=10) as response:
                success = response.status == 200
            
            # Restore original socket
            if original_socket:
                import socket as sock_module
                sock_module.socket = original_socket
            
            return success
            
        except Exception as e:
            # Restore original socket on error
            if original_socket:
                try:
                    import socket as sock_module
                    sock_module.socket = original_socket
                except:
                    pass
            
            print(f"✗ Error sending Telegram message: {e}")
            return False
    
    def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Send message to Telegram"""
        url = f"{self.api_url}/sendMessage"
        data = urllib.parse.urlencode({
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': parse_mode
        }).encode()
        
        return self._make_request(url, data)
    
    def send_top_configs(self, results: List[Tuple[str, float]], count: int = 10):
        """Send top configs to Telegram channel"""
        top_configs = results[:min(count, len(results))]
        
        if not top_configs:
            print("✗ No configs to send to Telegram")
            return False
        
        message = "🚀 *Top {} Configs with Lowest Ping*\n\n".format(len(top_configs))
        
        for i, (config, latency) in enumerate(top_configs, 1):
            message += f"{i}. `{config}`\n"
            message += f"   ⚡ Ping: {latency:.1f}ms\n\n"
        
        if self.send_message(message):
            print(f"✓ Sent top {len(top_configs)} configs to Telegram")
            return True
        else:
            print("✗ Failed to send to Telegram")
            return False

# Config Manager
class ConfigManager:
    def __init__(self, config_file: str = None):
        if config_file is None:
            config_dir = os.path.join(os.path.expanduser('~'), '.pingco')
            os.makedirs(config_dir, exist_ok=True)
            self.config_file = os.path.join(config_dir, 'config.json')
        else:
            self.config_file = config_file
        
        self.config = self._load_config()
    
    def _load_config(self) -> Dict:
        """Load config from file"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {'telegram_bots': []}
    
    def _save_config(self):
        """Save config to file"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            print(f"✗ Error saving config: {e}")
    
    def add_telegram_bot(self, api_key: str, chat_id: str, channel_name: str = ""):
        """Add telegram bot credentials"""
        bot = {
            'api_key': api_key,
            'chat_id': chat_id,
            'channel_name': channel_name
        }
        
        # Check if exists
        for existing in self.config['telegram_bots']:
            if existing['api_key'] == api_key and existing['chat_id'] == chat_id:
                return
        
        self.config['telegram_bots'].append(bot)
        self._save_config()
    
    def get_telegram_bots(self) -> List[Dict]:
        """Get all saved telegram bots"""
        return self.config.get('telegram_bots', [])
    
    def remove_telegram_bot(self, index: int):
        """Remove telegram bot by index"""
        if 0 <= index < len(self.config['telegram_bots']):
            self.config['telegram_bots'].pop(index)
            self._save_config()

# Interactive Menu
class InteractiveMenu:
    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.selected = 0
        self.in_submenu = False
    
    def display_main_menu(self) -> Optional[str]:
        """Display main menu and get user choice"""
        print("\n" + "="*60)
        print("📱 Telegram Integration")
        print("="*60)
        
        bots = self.config_manager.get_telegram_bots()
        
        options = []
        if bots:
            print("\n💾 Saved Bots:")
            for i, bot in enumerate(bots):
                channel = bot.get('channel_name', 'N/A')
                print(f"  {i+1}. {channel if channel else 'Bot ' + str(i+1)}")
                print(f"     API: {bot['api_key'][:20]}...")
                print(f"     Chat ID: {bot['chat_id']}")
                options.append(('saved', i))
        
        print("\n📝 Options:")
        print(f"  {len(options)+1}. Add New Bot (Custom)")
        options.append(('custom', None))
        
        print(f"  {len(options)+1}. Skip (No Telegram)")
        options.append(('skip', None))
        
        print(f"  {len(options)+1}. Exit")
        options.append(('exit', None))
        
        print("\n" + "="*60)
        
        while True:
            try:
                choice = input("Select option (1-{}): ".format(len(options))).strip()
                
                if not choice:
                    continue
                
                choice_num = int(choice) - 1
                if 0 <= choice_num < len(options):
                    return options[choice_num]
                else:
                    print("✗ Invalid choice, try again")
            except ValueError:
                print("✗ Please enter a number")
            except KeyboardInterrupt:
                return ('exit', None)
    
    def get_custom_bot(self) -> Optional[Dict]:
        """Get custom bot credentials from user"""
        print("\n" + "="*60)
        print("📝 Enter Telegram Bot Credentials")
        print("="*60)
        
        try:
            api_key = input("API Key: ").strip()
            if not api_key:
                print("✗ API key cannot be empty")
                return None
            
            chat_id = input("Chat ID: ").strip()
            if not chat_id:
                print("✗ Chat ID cannot be empty")
                return None
            
            channel_name = input("Channel Name (optional, e.g. @mychannel): ").strip()
            
            # Save for future use
            save = input("\n💾 Save these credentials? (y/n): ").strip().lower()
            if save == 'y':
                self.config_manager.add_telegram_bot(api_key, chat_id, channel_name)
                print("✓ Credentials saved")
            
            return {
                'api_key': api_key,
                'chat_id': chat_id,
                'channel_name': channel_name
            }
        except KeyboardInterrupt:
            return None
    
    def confirm_send(self) -> bool:
        """Ask user to confirm sending to Telegram"""
        print("\n" + "="*60)
        response = input("📤 Send top 10 configs to Telegram? (y/n): ").strip().lower()
        return response in ['y', 'yes']

# Main Application
class PingCoApp:
    def __init__(self):
        self.processor = ConfigProcessor(max_workers=50)
        self.fetcher = ConfigFetcher(max_workers=10)
        self.file_manager = FileManager()
    
    def run_single_ping(self, config: str):
        """Ping a single config"""
        print(f"Pinging config...")
        _, latency = self.processor.ping_config(config)
        
        if latency:
            print(f"✓ Ping: {latency:.1f}ms")
            print(f"Config: {config}")
        else:
            print("✗ Timeout - No response")
    
    def run_from_file(self, filepath: str, name: str, split_size: Optional[int], 
                      do_ping: bool, do_real_ping: bool, telegram_bot: Optional[TelegramBot]) -> List[Tuple[str, float]]:
        """Process configs from file"""
        print(f"Reading configs from: {filepath}")
        configs = self.file_manager.read_file(filepath)
        
        if not configs:
            print("No configs found in file")
            return []
        
        return self._process_configs(configs, name, split_size, do_ping, do_real_ping, telegram_bot)
    
    def run_from_sub(self, sub_input: str, name: str, split_size: Optional[int],
                     do_ping: bool, do_real_ping: bool, telegram_bot: Optional[TelegramBot]) -> List[Tuple[str, float]]:
        """Process configs from subscription URLs"""
        # Determine if input is a file or URL
        if os.path.isfile(sub_input):
            urls = self.file_manager.read_file(sub_input)
        elif sub_input.startswith('http'):
            urls = [sub_input]
        else:
            print("Invalid subscription input")
            return []
        
        print(f"Fetching from {len(urls)} URL(s)...")
        configs = self.fetcher.fetch_multiple(urls)
        
        if not configs:
            print("No configs fetched")
            return []
        
        return self._process_configs(configs, name, split_size, do_ping, do_real_ping, telegram_bot)
    
    def _process_configs(self, configs: List[str], name: str, split_size: Optional[int],
                         do_ping: bool, do_real_ping: bool, telegram_bot: Optional[TelegramBot]):
        """Core config processing logic"""
        print(f"Processing {len(configs)} configs...")
        
        # Remove duplicates
        print("Removing duplicates...")
        unique_configs = self.processor.remove_duplicates(configs)
        print(f"✓ {len(unique_configs)} unique configs")
        
        # Save all configs
        base_path = os.path.dirname(os.path.abspath(__file__))
        
        if split_size:
            self.file_manager.split_and_save(unique_configs, os.path.join(base_path, name), split_size)
        else:
            output_file = os.path.join(base_path, f"{name}.txt")
            self.file_manager.write_configs(output_file, unique_configs)
            print(f"✓ Saved all configs to: {output_file}")
        
        # Real ping if requested
        if do_real_ping:
            # First do ICMP ping to filter working configs
            print("\nStep 1: ICMP ping to filter working configs...")
            icmp_results = self.processor.ping_configs(unique_configs)
            
            if not icmp_results:
                print("✗ No configs responded to ICMP ping")
                return []
            
            print(f"✓ {len(icmp_results)} configs responded to ICMP")
            
            # Sort by ICMP latency
            icmp_sorted = self.processor.sort_by_latency(icmp_results)
            
            # Take top 50 for real ping test
            top_count = min(50, len(icmp_sorted))
            print(f"\nStep 2: Real ping test on top {top_count} configs...")
            
            top_configs = [config for config, _ in icmp_sorted[:top_count]]
            results = self.processor.real_ping_configs(top_configs)
            
            if results:
                print(f"✓ {len(results)} configs passed real ping test")
                
                # Sort by latency
                print("Sorting by latency...")
                sorted_results = self.processor.sort_by_latency(results)
                
                # Save results
                output_file = os.path.join(base_path, f"{name}_realping.txt")
                self.file_manager.write_results(output_file, sorted_results)
                print(f"✓ Saved real ping results to: {output_file}")
                
                # Show top 10
                print("\n" + "="*80)
                print("📊 Top 10 Configs (Lowest Real Ping)")
                print("="*80)
                for i, (config, latency) in enumerate(sorted_results[:10], 1):
                    print(f"\n{i}. Ping: {latency:.1f}ms")
                    print(f"   {config}")
                
                print("\n" + "="*80)
                
                return sorted_results
            else:
                print("✗ No configs passed real ping test")
                return []
        
        # Regular ICMP ping if requested
        elif do_ping:
            print("\nStarting ICMP ping test...")
            results = self.processor.ping_configs(unique_configs)
            
            if results:
                print(f"✓ {len(results)} configs responded")
                
                # Sort by latency
                print("Sorting by latency...")
                sorted_results = self.processor.sort_by_latency(results)
                
                # Save ping results with progress bar
                if split_size:
                    # Save for each split file
                    total_files = (len(unique_configs) + split_size - 1) // split_size
                    progress = ProgressBar(total_files, "Saving ping results")
                    
                    for i in range(total_files):
                        start_idx = i * split_size
                        end_idx = start_idx + split_size
                        chunk_configs = set(unique_configs[start_idx:end_idx])
                        chunk_results = [(c, l) for c, l in sorted_results if c in chunk_configs]
                        
                        if chunk_results:
                            filename = os.path.join(base_path, f"{name}{i+1}_icmp.txt")
                            self.file_manager.write_results(filename, chunk_results)
                        progress.update(1)
                else:
                    output_file = os.path.join(base_path, f"{name}_icmp.txt")
                    self.file_manager.write_results(output_file, sorted_results)
                    print(f"✓ Saved ping results to: {output_file}")
                
                # Show top 10 with full configs
                print("\n" + "="*80)
                print("📊 Top 10 Configs (Lowest Ping)")
                print("="*80)
                for i, (config, latency) in enumerate(sorted_results[:10], 1):
                    print(f"\n{i}. Ping: {latency:.1f}ms")
                    print(f"   {config}")
                
                print("\n" + "="*80)
                
                # Return sorted results for interactive menu
                return sorted_results
            else:
                print("✗ No configs responded to ping")
                return []
        
        # Clean up memory
        gc.collect()
        return []

def main():
    parser = argparse.ArgumentParser(
        description='PingCo - VLESS/VMESS Config Ping Tester & Manager',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('-conf', '--config', 
                       help='Single config string or file path with configs')
    parser.add_argument('-sub', '--subscription',
                       help='Subscription URL or file with URLs')
    parser.add_argument('-name', '--name', default='configs',
                       help='Base name for output files (default: configs)')
    parser.add_argument('-split', '--split', type=int,
                       help='Split configs into files of N configs each')
    parser.add_argument('-icmp', '--icmp', action='store_true',
                       help='Perform ICMP ping test')
    parser.add_argument('-hp', '--real-ping', action='store_true',
                       help='Perform real ping test (through proxy to Google/Cloudflare)')
    parser.add_argument('-telbot', '--telegram-bot', nargs=2, metavar=('TOKEN', 'CHAT_ID'),
                       help='Telegram bot token and chat ID for notifications')
    
    args = parser.parse_args()
    
    # Validate arguments
    if not args.config and not args.subscription:
        parser.print_help()
        return
    
    # Initialize app and config manager
    app = PingCoApp()
    config_mgr = ConfigManager()
    connection_mgr = None
    
    # Setup Telegram bot if provided via args
    telegram_bot = None
    if args.telegram_bot:
        proxy = TelegramBot.detect_system_proxy()
        if proxy:
            print(f"✓ Detected system proxy: {proxy}")
        telegram_bot = TelegramBot(args.telegram_bot[0], args.telegram_bot[1], proxy)
    
    try:
        results = []
        
        # Single config mode
        if args.config and not os.path.isfile(args.config):
            if ConfigUtils.is_valid_config(args.config):
                app.run_single_ping(args.config)
            else:
                print("Invalid config format")
            return
        
        # File/subscription mode
        if args.config:
            results = app.run_from_file(args.config, args.name, args.split, args.icmp, args.real_ping, telegram_bot)
        elif args.subscription:
            results = app.run_from_sub(args.subscription, args.name, args.split, args.icmp, args.real_ping, telegram_bot)
        
        # Interactive menu if we have ping results and no telegram bot was specified via args
        if results and not args.telegram_bot:
            menu = InteractiveMenu(config_mgr)
            
            if menu.confirm_send():
                choice = menu.display_main_menu()
                
                if choice:
                    choice_type, choice_data = choice
                    
                    if choice_type == 'saved':
                        # Use saved bot
                        bot_info = config_mgr.get_telegram_bots()[choice_data]
                        print(f"\n✓ Using saved bot: {bot_info.get('channel_name', 'Bot')}")
                        
                        # Try to send with retry mechanism
                        while True:
                            # First try with system proxy
                            proxy = TelegramBot.detect_system_proxy()
                            
                            if not proxy:
                                # No system proxy, try to connect via best config
                                print("\n⚠️  No system proxy detected!")
                                print("🔌 Attempting to connect via best config...")
                                
                                connection_mgr = ConfigConnectionManager(results)
                                conn_result = connection_mgr.try_connect_with_configs(max_attempts=1)
                                
                                if conn_result:
                                    proxy = conn_result['proxy']
                                    print(f"✓ Connected via: {conn_result['config'][:70]}...")
                                else:
                                    print("✗ Failed to connect via configs")
                            else:
                                print(f"✓ Detected proxy: {proxy.get('host', '127.0.0.1')}:{proxy.get('port', 'unknown')}")
                            
                            # Check PySocks
                            if proxy and not HAS_SOCKS:
                                print("✗ PySocks is not installed!")
                                print("  Install: pip3 install PySocks --break-system-packages")
                                break
                            
                            bot = TelegramBot(bot_info['api_key'], bot_info['chat_id'], proxy)
                            success = bot.send_top_configs(results, count=10)
                            
                            if success:
                                break
                            else:
                                # Failed to send
                                print("\n" + "="*60)
                                print("❌ Failed to send to Telegram")
                                print("="*60)
                                retry_choice = input("\nOptions:\n  1. Retry (after connecting VPN)\n  2. Exit\n\nSelect (1-2): ").strip()
                                
                                if retry_choice == '1':
                                    print("\n⏳ Retrying...")
                                    # Disconnect previous connection if any
                                    if connection_mgr:
                                        connection_mgr.disconnect()
                                        connection_mgr = None
                                    continue
                                else:
                                    print("\n✓ Cancelled")
                                    break
                        
                    elif choice_type == 'custom':
                        # Get custom bot
                        bot_info = menu.get_custom_bot()
                        if bot_info:
                            # Try to send with retry mechanism
                            while True:
                                # First try with system proxy
                                proxy = TelegramBot.detect_system_proxy()
                                
                                if not proxy:
                                    # No system proxy, try to connect via best config
                                    print("\n⚠️  No system proxy detected!")
                                    print("🔌 Attempting to connect via best config...")
                                    
                                    connection_mgr = ConfigConnectionManager(results)
                                    conn_result = connection_mgr.try_connect_with_configs(max_attempts=1)
                                    
                                    if conn_result:
                                        proxy = conn_result['proxy']
                                        print(f"✓ Connected via: {conn_result['config'][:70]}...")
                                    else:
                                        print("✗ Failed to connect via configs")
                                else:
                                    print(f"✓ Detected proxy: {proxy.get('host', '127.0.0.1')}:{proxy.get('port', 'unknown')}")
                                
                                # Check PySocks
                                if proxy and not HAS_SOCKS:
                                    print("✗ PySocks is not installed!")
                                    print("  Install: pip3 install PySocks --break-system-packages")
                                    break
                                
                                bot = TelegramBot(bot_info['api_key'], bot_info['chat_id'], proxy)
                                success = bot.send_top_configs(results, count=10)
                                
                                if success:
                                    break
                                else:
                                    # Failed to send
                                    print("\n" + "="*60)
                                    print("❌ Failed to send to Telegram")
                                    print("="*60)
                                    retry_choice = input("\nOptions:\n  1. Retry (after connecting VPN)\n  2. Exit\n\nSelect (1-2): ").strip()
                                    
                                    if retry_choice == '1':
                                        print("\n⏳ Retrying...")
                                        # Disconnect previous connection if any
                                        if connection_mgr:
                                            connection_mgr.disconnect()
                                            connection_mgr = None
                                        continue
                                    else:
                                        print("\n✓ Cancelled")
                                        break
                    
                    elif choice_type == 'skip':
                        print("\n✓ Skipping Telegram send")
                    
                    elif choice_type == 'exit':
                        print("\n👋 Goodbye!")
                        return
        
        # If telegram bot was specified via args, send automatically
        elif results and telegram_bot:
            telegram_bot.send_top_configs(results, count=10)
        
        print("\n✓ All operations completed successfully!")
        
    except KeyboardInterrupt:
        print("\n\n⚠ Interrupted by user")
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Cleanup connection if any
        if connection_mgr:
            connection_mgr.disconnect()
        # Final cleanup
        gc.collect()

if __name__ == '__main__':
    main()
