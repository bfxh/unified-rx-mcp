//! lse-engine CLI entry: reads JSON commands from stdin, writes JSON responses
//! to stdout, persists state after each command.

use lse_engine::run_cli;

fn main() {
    if let Err(e) = run_cli() {
        eprintln!("lse-engine error: {}", e);
        std::process::exit(1);
    }
}
