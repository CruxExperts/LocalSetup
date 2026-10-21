# Ansible Troubleshooting Guide

## Connection Issues

### "Permission denied" / SSH Key Issues

```bash
# Test SSH manually first
ssh -v user@host

# Check key permissions (must be 600)
chmod 600 ~/.ssh/id_*
chmod 644 ~/.ssh/*.pub
chmod 700 ~/.ssh

# Specify key explicitly
ansible all -i inventory -m ping --private-key=~/.ssh/mykey

# Debug Ansible connection
ansible all -i inventory -m ping -vvv
```

### "Host key verification failed"

Obtain the expected host-key fingerprint from a trusted provider console or other
out-of-band channel. Compare that fingerprint before accepting any scanned key.

```bash
# Scan only after verifying the out-of-band fingerprint matches this host.
ssh-keyscan -H hostname >> ~/.ssh/known_hosts

# Temporary, high-risk diagnostic only: never leave this disabled in inventory
# or ansible.cfg after diagnosing a connection problem.
ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i inventory/hosts.yml playbooks/site.yml
```

### "No route to host" / Network Issues

```bash
# Check basic connectivity
ping hostname
nc -zv hostname 22

# Check firewall
sudo ufw status
sudo iptables -L

# Try with specific port
ansible all -i inventory -m ping -e "ansible_port=2222"
```

### Python Interpreter Not Found

```yaml
# Specify in inventory
all:
  vars:
    ansible_python_interpreter: /usr/bin/python3

# Or per-host
myhost:
  ansible_python_interpreter: /usr/bin/python3
```

## Authentication Issues

### "SUDO password required"

```bash
# Option 1: Ask for password
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --ask-become-pass

# Option 2: Use passwordless sudo on target
# /etc/sudoers.d/myuser:
myuser ALL=(ALL) NOPASSWD:ALL

# Option 3: Set in inventory
all:
  vars:
    ansible_become_pass: "{{ vault_sudo_password }}"
```

### Vault Password Issues

```bash
# Interactive password prompt
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --ask-vault-pass

# Password file
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --vault-password-file ~/.vault_pass

# Multiple vault passwords
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --vault-id dev@~/.vault_dev --vault-id prod@~/.vault_prod

# View encrypted file
ansible-vault view group_vars/all/vault.yml

# Edit encrypted file
ansible-vault edit group_vars/all/vault.yml

# Rekey (change password)
ansible-vault rekey group_vars/all/vault.yml
```

## Module Errors

### "Module not found"

```bash
# Use FQCN
# Wrong: ufw
# Right: community.general.ufw

# Install required example collections
ansible-galaxy collection install -r collections/requirements.yml

# List installed collections
ansible-galaxy collection list
```

### "Requires X library"

```bash
# Install on target machine
ansible myhost -m apt -a "name=python3-xyz state=present" --become

# Or install via pip on target
ansible myhost -m pip -a "name=xyz state=present"
```

## Playbook Errors

### Syntax Errors

```bash
# Check syntax
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --syntax-check

# Lint playbook
uv tool install ansible-lint
ansible-lint playbooks/site.yml
```

### Variable Undefined

```yaml
# Check if defined before use
- debug:
    msg: "{{ my_var }}"
  when: my_var is defined

# Provide default value
- debug:
    msg: "{{ my_var | default('fallback') }}"
```

### Template Errors

```bash
# Test template rendering
ansible localhost -m template -a "src=template.j2 dest=/dev/stdout"

# Common issues:
# - Missing variable: {{ undefined_var }}
# - Wrong filter: {{ var | wrong_filter }}
# - Syntax: {% if %}...missing{% endif %}
```

## Debugging

### Verbose Output

```bash
# Increasing verbosity levels
ansible-playbook -i inventory/hosts.yml playbooks/site.yml -v      # Basic
ansible-playbook -i inventory/hosts.yml playbooks/site.yml -vv     # More
ansible-playbook -i inventory/hosts.yml playbooks/site.yml -vvv    # Connection debug
ansible-playbook -i inventory/hosts.yml playbooks/site.yml -vvvv   # Maximum
```

### Step Through Tasks

```bash
# Step mode - confirm each task
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --step

# Start at specific task
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --start-at-task="Install nginx"

# List tasks without running
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --list-tasks

# List hosts that would be affected
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --list-hosts
```

### Debug Module

```yaml
# Print variable
- ansible.builtin.debug:
    var: my_var

# Print message
- ansible.builtin.debug:
    msg: "Value is {{ my_var }}"

# Print all facts
- ansible.builtin.debug:
    var: ansible_facts

# Conditional debug
- ansible.builtin.debug:
    msg: "This is production!"
  when: env == "production"
```

### Register and Check

```yaml
- name: Run command
  ansible.builtin.command: /opt/script.sh
  register: result
  ignore_errors: yes

- name: Show result
  ansible.builtin.debug:
    var: result

- name: Show specific parts
  ansible.builtin.debug:
    msg: |
      stdout: {{ result.stdout }}
      stderr: {{ result.stderr }}
      rc: {{ result.rc }}
```

## Performance Issues

### Slow Playbook Execution

```yaml
# Disable fact gathering if not needed
- hosts: all
  gather_facts: no

# Gather only needed facts
- hosts: all
  gather_subset:
    - network

# Use pipelining (faster but needs requiretty disabled)
# ansible.cfg:
[ssh_connection]
pipelining = True

# Increase parallelism
# ansible.cfg:
[defaults]
forks = 20
```

### Slow SSH Connections

```bash
# Use ControlMaster (connection reuse)
# ansible.cfg:
[ssh_connection]
ssh_args = -o ControlMaster=auto -o ControlPersist=60s
```

## Common Error Messages

### "Aborting, target uses selinux but python bindings aren't installed"

```yaml
- name: Install SELinux Python bindings
  ansible.builtin.yum:
    name:
      - libselinux-python3
      - python3-policycoreutils
    state: present
```

### "msg: Destination not writable"

```yaml
# Add become: yes
- name: Copy file to protected location
  ansible.builtin.copy:
    src: file.txt
    dest: /etc/myfile.txt
  become: yes
```

### "Failed to connect to the host via ssh: Connection timed out"

```yaml
# Increase timeout
# In inventory or playbook:
ansible_ssh_timeout: 30

# Or command line:
ansible-playbook -i inventory/hosts.yml playbooks/site.yml -T 30
```

### "Error while fetching server API version"

Usually Docker-related:
```yaml
# Ensure user is in docker group
- name: Add user to docker group
  ansible.builtin.user:
    name: "{{ ansible_user }}"
    groups: docker
    append: yes
  become: yes
# Note: Requires logout/login to take effect
```

## Recovery

### Retry Failed Hosts

```bash
# Retry files are disabled by default. Enable them explicitly when wanted.
# In ansible.cfg: retry_files_enabled = true
# Ansible then writes or overwrites the configured retry file after failures.
ansible-playbook -i inventory/hosts.yml playbooks/site.yml --limit @playbooks/site.retry
```
### Rollback

```yaml
# Use block/rescue
- name: Deploy with rollback
  block:
    - name: Deploy new version
      ansible.builtin.unarchive:
        src: app-current.tar.gz
        dest: /opt/app
    - name: Restart service
      ansible.builtin.systemd:
        name: myapp
        state: restarted
  rescue:
    - name: Rollback to previous version
      ansible.builtin.command: /opt/rollback.sh
    - name: Notify failure
      ansible.builtin.debug:
        msg: "Deployment failed, rolled back"
```
