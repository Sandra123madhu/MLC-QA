import os
import subprocess
import sys
import venv

def run_command(command, cwd=None):
    print(f"Running: {' '.join(command)}")
    try:
        subprocess.run(command, check=True, cwd=cwd)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {e}")
        return False

def main():
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(backend_dir)

    print("--- MLC QA Local Setup ---")

    # 1. Create Virtual Environment
    venv_dir = os.path.join(backend_dir, "venv")
    if not os.path.exists(venv_dir):
        print(f"Creating virtual environment in {venv_dir}...")
        venv.create(venv_dir, with_pip=True)
    else:
        print("Virtual environment already exists.")

    # 2. Determine python/pip paths in venv
    if os.name == 'nt': # Windows
        python_exe = os.path.join(venv_dir, "Scripts", "python.exe")
        pip_exe = os.path.join(venv_dir, "Scripts", "pip.exe")
    else: # Linux/Mac
        python_exe = os.path.join(venv_dir, "bin", "python")
        pip_exe = os.path.join(venv_dir, "bin", "pip")

    # 3. Component Check
    if not os.path.exists("requirements.txt"):
        print("Error: requirements.txt not found in backend directory.")
        return

    # 4. Install Dependencies
    print("Installing dependencies...")
    if not run_command([pip_exe, "install", "-r", "requirements.txt"]):
        print("Failed to install dependencies.")
        return

    # 5. Check for Environment Variables
    print("\n--- Environment Check ---")
    env_missing = False
    if not os.environ.get("SUPABASE_URL"):
        print("Warning: SUPABASE_URL environment variable is missing.")
        env_missing = True
    if not os.environ.get("SUPABASE_KEY"):
        print("Warning: SUPABASE_KEY environment variable is missing.")
        env_missing = True
    
    if env_missing:
        print("\nNote: You need to set your Supabase credentials for the backend to function.")
        print("You can set them in your terminal before running the server, or add them to a .env file (if using python-dotenv).")

    print("\nSetup complete! You can now start the server using 'start_local.bat'.")

if __name__ == "__main__":
    main()
