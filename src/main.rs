// Ultra-fast VLESS config processor in Rust
// Optimized for maximum throughput and minimal memory usage

use rayon::prelude::*;
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::Write;
use std::path::Path;
use tokio::task;

// Main config structure - zero-copy string slices where possible
#[derive(Debug, Clone, Hash, Eq, PartialEq)]
struct Config {
    raw: String,
    transport: TransportType,
}

// Transport types as enum for fastest matching
#[derive(Debug, Clone, Copy, Hash, Eq, PartialEq)]
enum TransportType {
    WebSocket,
    Grpc,
    Tcp,
    Tls,
    XHttp,
}

impl TransportType {
    // O(1) conversion to string
    fn as_str(&self) -> &'static str {
        match self {
            Self::WebSocket => "ws",
            Self::Grpc => "grpc",
            Self::Tcp => "tcp",
            Self::Tls => "tls",
            Self::XHttp => "xhttp",
        }
    }
}

// Fast base64 detection without regex - O(1) per character
#[inline(always)]
fn is_base64_char(c: u8) -> bool {
    matches!(c, b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'+' | b'/' | b'=')
}

// Decode base64 iteratively - handles nested encoding
fn decode_base64(input: &str) -> String {
    let mut result = input.to_string();
    
    // Try up to 5 layers of base64 encoding
    for _ in 0..5 {
        // Quick check if it looks like base64
        if result.len() < 20 || result.starts_with("vless://") || result.starts_with("vmess://") {
            break;
        }
        
        // Check if all characters are base64
        if !result.bytes().all(|b| is_base64_char(b) || b.is_ascii_whitespace()) {
            break;
        }
        
        // Try to decode
        match base64::decode(result.trim()) {
            Ok(decoded) => {
                match String::from_utf8(decoded) {
                    Ok(s) if s != result => result = s,
                    _ => break,
                }
            }
            Err(_) => break,
        }
    }
    
    result
}

// Extract transport type from config - O(1) time complexity
// Uses fast substring search instead of regex
#[inline(always)]
fn extract_transport(config: &str) -> TransportType {
    // Find query string
    let query = match config.find('?') {
        Some(pos) => &config[pos..],
        None => return TransportType::Tcp,
    };
    
    // Fast substring search for type parameter
    if let Some(type_pos) = query.find("type=") {
        let type_start = type_pos + 5;
        let type_end = query[type_start..]
            .find('&')
            .map(|p| type_start + p)
            .unwrap_or(query.len());
        
        let transport = &query[type_start..type_end];
        
        // Match using first few characters for speed
        return match transport {
            t if t.starts_with("ws") => TransportType::WebSocket,
            t if t.starts_with("grpc") => TransportType::Grpc,
            t if t.starts_with("xhttp") || t.starts_with("httpupgrade") => TransportType::XHttp,
            _ => TransportType::Tcp,
        };
    }
    
    // Check for TLS
    if query.contains("security=tls") {
        return TransportType::Tls;
    }
    
    TransportType::Tcp
}

// Async download with timeout - runs all downloads concurrently
async fn fetch_url(url: &str) -> Result<Vec<String>, Box<dyn std::error::Error>> {
    println!("📥 Downloading: {}", url);
    
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()?;
    
    let response = client.get(url)
        .header("User-Agent", "Mozilla/5.0")
        .send()
        .await?;
    
    let body = response.text().await?;
    let decoded = decode_base64(&body);
    
    // Split and filter in one pass
    let configs: Vec<String> = decoded
        .lines()
        .filter(|line| line.starts_with("vless://"))
        .map(|s| s.to_string())
        .collect();
    
    println!("   ✓ Found {} VLESS configs", configs.count());
    Ok(configs)
}

// Download all URLs concurrently - maximum parallelism
async fn fetch_all(urls: Vec<String>) -> Vec<String> {
    println!("\n{}", "=".repeat(60));
    println!("📥 Fetching configs from all URLs...");
    println!("{}", "=".repeat(60));
    
    // Create futures for all downloads
    let tasks: Vec<_> = urls
        .into_iter()
        .map(|url| task::spawn(fetch_url(url.clone())))
        .collect();
    
    // Wait for all to complete
    let mut all_configs = Vec::new();
    for task in tasks {
        if let Ok(Ok(configs)) = task.await {
            all_configs.extend(configs);
        }
    }
    
    println!("\n✓ Total downloaded: {}", all_configs.len());
    all_configs
}

// Deduplicate using HashSet - O(1) average case per operation
fn deduplicate(configs: Vec<String>) -> Vec<String> {
    println!("\n🔄 Removing duplicates...");
    
    let original_count = configs.len();
    
    // Use hashbrown for faster hashing
    let unique: std::collections::HashSet<_> = configs.into_iter().collect();
    let unique_vec: Vec<_> = unique.into_iter().collect();
    
    println!("✓ Unique configs: {} (removed {} duplicates)", 
             unique_vec.len(), 
             original_count - unique_vec.len());
    
    unique_vec
}

// Categorize configs using parallel processing
fn categorize(configs: Vec<String>) -> HashMap<TransportType, Vec<String>> {
    println!("\n📊 Categorizing by transport type...");
    
    // Process in parallel using rayon
    let categorized: HashMap<TransportType, Vec<String>> = configs
        .par_iter()
        .fold(
            || HashMap::new(),
            |mut map, config| {
                let transport = extract_transport(config);
                map.entry(transport)
                    .or_insert_with(Vec::new)
                    .push(config.clone());
                map
            }
        )
        .reduce(
            || HashMap::new(),
            |mut a, b| {
                for (k, mut v) in b {
                    a.entry(k).or_insert_with(Vec::new).append(&mut v);
                }
                a
            }
        );
    
    // Print statistics
    for (transport, configs) in &categorized {
        println!("   {}: {} configs", transport.as_str().to_uppercase(), configs.len());
    }
    
    categorized
}

// Save all configs to file
fn save_all_configs(configs: &[String], output_dir: &Path) -> std::io::Result<()> {
    let filepath = output_dir.join("all_vless_config.txt");
    let mut file = File::create(&filepath)?;
    
    for config in configs {
        writeln!(file, "{}", config)?;
    }
    
    println!("\n✓ Saved all configs to: all_vless_config.txt");
    Ok(())
}

// Save categorized configs
fn save_by_transport(
    categories: &HashMap<TransportType, Vec<String>>,
    output_dir: &Path
) -> std::io::Result<()> {
    println!("\n💾 Saving categorized configs...");
    
    for (transport, configs) in categories {
        let filename = format!("vless_{}.txt", transport.as_str());
        let filepath = output_dir.join(&filename);
        let mut file = File::create(&filepath)?;
        
        for config in configs {
            writeln!(file, "{}", config)?;
        }
        
        println!("   ✓ {} ({} configs)", filename, configs.len());
    }
    
    Ok(())
}

// Split configs into chunks
fn split_configs(configs: &[String], split_size: usize, output_dir: &Path) -> std::io::Result<()> {
    println!("\n✂️  Splitting into {}-config files...", split_size);
    
    let chunks: Vec<_> = configs.chunks(split_size).collect();
    
    for (i, chunk) in chunks.iter().enumerate() {
        let filename = format!("vless_config_{}.txt", i + 1);
        let filepath = output_dir.join(&filename);
        let mut file = File::create(&filepath)?;
        
        for config in *chunk {
            writeln!(file, "{}", config)?;
        }
    }
    
    println!("   ✓ Created {} split files", chunks.len());
    Ok(())
}

// Generate README with raw links
fn update_readme(output_dir: &Path, repo_url: &str) -> std::io::Result<()> {
    println!("\n📝 Updating README.md...");
    
    // Get all txt files
    let mut files: Vec<_> = fs::read_dir(output_dir)?
        .filter_map(Result::ok)
        .filter(|e| e.path().extension().and_then(|s| s.to_str()) == Some("txt"))
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    
    files.sort();
    
    // Build README content
    let mut readme = format!(
        "# 🚀 VLESS Configs Repository\n\n\
         Auto-updated every 6 hours with fresh VLESS configurations.\n\n\
         ## 📊 Statistics\n\n\
         - **Total Files**: {}\n\
         - **Last Update**: Auto-generated\n\
         - **Update Frequency**: Every 6 hours\n\n\
         ## 📁 Available Files\n\n\
         ### All Configs\n\n",
        files.len()
    );
    
    // Add raw links
    for file in &files {
        let raw_url = format!("{}/raw/main/configs/{}", repo_url, file);
        readme.push_str(&format!("- [{}]({})\n", file, raw_url));
    }
    
    readme.push_str(
        "\n## 🔗 How to Use\n\n\
         Copy any raw link above and add it as a subscription in your V2Ray client.\n\n\
         ### Example:\n\
         ```\n\
         https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/configs/vless_config_1.txt\n\
         ```\n\n\
         ## ⚙️ Transport Types\n\n\
         Configs are categorized by transport protocol:\n\
         - **WS**: WebSocket\n\
         - **gRPC**: Google RPC\n\
         - **TCP**: Standard TCP\n\
         - **TLS**: With TLS encryption\n\
         - **XHTTP**: HTTP Upgrade\n\n\
         ---\n\n\
         *Auto-updated by GitHub Actions*\n"
    );
    
    // Write README
    let readme_path = output_dir.parent().unwrap().join("README.md");
    fs::write(readme_path, readme)?;
    
    println!("   ✓ README.md updated with raw links");
    Ok(())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Parse arguments
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("Usage: {} <subscriptions_file>", args[0]);
        std::process::exit(1);
    }
    
    let subs_file = &args[1];
    
    // Read subscription URLs
    let urls: Vec<String> = fs::read_to_string(subs_file)?
        .lines()
        .filter(|line| !line.trim().is_empty() && !line.starts_with('#'))
        .map(|s| s.to_string())
        .collect();
    
    println!("📋 Found {} subscription URLs", urls.len());
    
    // Create output directory
    let output_dir = Path::new("configs");
    fs::create_dir_all(output_dir)?;
    
    // Download all configs
    let all_configs = fetch_all(urls).await;
    
    if all_configs.is_empty() {
        eprintln!("\n✗ No configs downloaded!");
        std::process::exit(1);
    }
    
    // Deduplicate
    let unique_configs = deduplicate(all_configs);
    
    // Categorize
    let categories = categorize(unique_configs.clone());
    
    // Save files
    save_all_configs(&unique_configs, output_dir)?;
    save_by_transport(&categories, output_dir)?;
    split_configs(&unique_configs, 300, output_dir)?;
    
    // Update README
    let repo_url = "https://github.com/Matt-Ranaei/vless"; // Change this
    update_readme(output_dir, repo_url)?;
    
    println!("\n{}", "=".repeat(60));
    println!("✅ All done!");
    println!("{}", "=".repeat(60));
    println!("📁 Output directory: configs/");
    println!("📊 Total unique configs: {}", unique_configs.len());
    println!("📝 README.md updated with raw links");
    println!("{}", "=".repeat(60));
    
    Ok(())
}
