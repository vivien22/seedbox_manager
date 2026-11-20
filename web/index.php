<?php
$config = require '/media/sdt1/bibien/private/seedbox-manager/config.php';

$baseDir   = dirname($config['queue_dir']);
$statusDir = isset($config['status_dir']) ? $config['status_dir'] : $baseDir . '/status';
$stateDir  = isset($config['state_dir'])  ? $config['state_dir']  : $baseDir . '/state';
$commandsDir = $baseDir . '/commands';

const MAX_LOGIN_FAILURES = 5;
const LOGIN_WINDOW = 900;      // 15 min
const LOGIN_LOCK = 900;        // blocage 15 min
const PI_STALE_AFTER = 600;    // la Pi publie au moins toutes les 2 min
const SEEDBOX_STALE_AFTER = 120;

session_name('vivien_manager');
if (PHP_VERSION_ID >= 70300) {
    session_set_cookie_params(array(
        'lifetime' => 0, 'path' => '/', 'secure' => !empty($_SERVER['HTTPS']),
        'httponly' => true, 'samesite' => 'Strict',
    ));
} else {
    session_set_cookie_params(0, '/', '', !empty($_SERVER['HTTPS']), true);
}
session_start();

header('X-Content-Type-Options: nosniff');
header('X-Frame-Options: DENY');
header('Referrer-Policy: no-referrer');
header("Content-Security-Policy: default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'");

if (empty($_SESSION['csrf'])) {
    $_SESSION['csrf'] = bin2hex(random_bytes(32));
}

/* ------------------------------------------------------------ utilitaires */

function h($value)
{
    return htmlspecialchars((string)$value, ENT_QUOTES, 'UTF-8');
}

function read_json_file($path)
{
    if (!is_file($path)) {
        return null;
    }
    $raw = @file_get_contents($path);
    if ($raw === false) {
        return null;
    }
    $data = json_decode($raw, true);
    return is_array($data) ? $data : null;
}

function fmt_bytes($bytes)
{
    $bytes = (float)$bytes;
    $units = array('o', 'Ko', 'Mo', 'Go', 'To');
    $i = 0;
    while ($bytes >= 1024 && $i < count($units) - 1) {
        $bytes /= 1024;
        $i++;
    }
    return $i === 0 ? sprintf('%d o', $bytes) : sprintf('%.1f %s', $bytes, $units[$i]);
}

function fmt_speed($bytesPerSecond)
{
    return $bytesPerSecond > 0 ? fmt_bytes($bytesPerSecond) . '/s' : '';
}

function fmt_duration($seconds)
{
    if ($seconds === null || $seconds <= 0) {
        return '';
    }
    $seconds = (int)$seconds;
    if ($seconds < 60) {
        return $seconds . ' s';
    }
    if ($seconds < 3600) {
        return floor($seconds / 60) . ' min';
    }
    if ($seconds < 86400) {
        return floor($seconds / 3600) . ' h ' . sprintf('%02d', floor(($seconds % 3600) / 60));
    }
    return floor($seconds / 86400) . ' j ' . floor(($seconds % 86400) / 3600) . ' h';
}

function fmt_ago($timestamp)
{
    if (!$timestamp) {
        return 'jamais';
    }
    $delta = time() - (int)$timestamp;
    if ($delta < 10) {
        return "à l'instant";
    }
    return 'il y a ' . fmt_duration($delta);
}

function progress_bar($percent, $class = '')
{
    $percent = max(0, min(100, (float)$percent));
    return '<div class="bar ' . h($class) . '"><span style="width:' . sprintf('%.1f', $percent)
        . '%"></span></div>';
}

/* ------------------------------------------------- limitation des connexions */

function login_attempts_file($stateDir)
{
    if (!is_dir($stateDir)) {
        @mkdir($stateDir, 0700, true);
    }
    return $stateDir . '/login_attempts.json';
}

/** Ouvre le fichier des tentatives sous verrou et appelle $callback(&$data). */
function with_login_attempts($stateDir, $callback)
{
    $handle = @fopen(login_attempts_file($stateDir), 'c+');
    if ($handle === false) {
        $data = array();
        return $callback($data);
    }
    flock($handle, LOCK_EX);
    $raw = stream_get_contents($handle);
    $data = $raw ? json_decode($raw, true) : array();
    if (!is_array($data)) {
        $data = array();
    }

    $now = time();
    foreach ($data as $key => $entry) {
        if ($entry['first'] + LOGIN_WINDOW < $now && $entry['locked_until'] < $now) {
            unset($data[$key]);
        }
    }

    $result = $callback($data);

    ftruncate($handle, 0);
    rewind($handle);
    fwrite($handle, json_encode($data));
    fflush($handle);
    flock($handle, LOCK_UN);
    fclose($handle);
    return $result;
}

function client_key()
{
    $ip = isset($_SERVER['REMOTE_ADDR']) ? $_SERVER['REMOTE_ADDR'] : 'inconnu';
    return hash('sha256', $ip);
}

function login_locked_for($stateDir)
{
    $key = client_key();
    return with_login_attempts($stateDir, function (&$data) use ($key) {
        if (isset($data[$key]) && $data[$key]['locked_until'] > time()) {
            return $data[$key]['locked_until'] - time();
        }
        return 0;
    });
}

function register_login_failure($stateDir)
{
    $key = client_key();
    with_login_attempts($stateDir, function (&$data) use ($key) {
        $now = time();
        if (!isset($data[$key])) {
            $data[$key] = array('count' => 0, 'first' => $now, 'locked_until' => 0);
        }
        $data[$key]['count']++;
        if ($data[$key]['count'] >= MAX_LOGIN_FAILURES) {
            $data[$key]['locked_until'] = $now + LOGIN_LOCK;
            $data[$key]['count'] = 0;
            $data[$key]['first'] = $now;
        }
    });
}

function clear_login_failures($stateDir)
{
    $key = client_key();
    with_login_attempts($stateDir, function (&$data) use ($key) {
        unset($data[$key]);
    });
}

/* -------------------------------------------------------------- affichage */

function stage_label($torrent, $pi)
{
    $current = isset($pi['manifest']['torrent_id']) ? $pi['manifest']['torrent_id'] : null;
    $piFresh = $pi && isset($pi['updated_at']) && time() - $pi['updated_at'] < PI_STALE_AFTER;

    switch ($torrent['stage']) {
        case 'downloading':
            if (in_array($torrent['state'], array('Paused', 'Queued'), true)) {
                return array('En pause sur la seedbox', 'muted');
            }
            if ($torrent['state'] === 'Error') {
                return array('Erreur Deluge', 'bad');
            }
            return array('Téléchargement sur la seedbox', 'active');
        case 'organizing':
            return array('Organisation des fichiers', 'active');
        case 'failed':
            return array("Échec de l'organisation", 'bad');
        case 'ready_for_pi':
            if ($piFresh && $current === $torrent['id']) {
                return array('Transfert vers la Raspberry Pi', 'active');
            }
            return array('En attente de la Raspberry Pi', 'muted');
        case 'on_pi':
            return array('Sur la Raspberry Pi', 'good');
        case 'removing':
            return array('Suppression de la seedbox en cours…', 'muted');
        default:
            return array('Terminé (partage)', 'good');
    }
}

function render_seedbox($seedbox, $pi, $csrf)
{
    ob_start();
    echo '<section><h2>Seedbox</h2>';

    if ($seedbox === null) {
        echo '<p class="muted">Aucun état disponible : status_dump.py tourne-t-il dans seedbox-loop.sh ?</p></section>';
        return ob_get_clean();
    }

    $age = time() - (int)$seedbox['updated_at'];
    if ($age > SEEDBOX_STALE_AFTER) {
        echo '<div class="notice error">État non mis à jour depuis ' . h(fmt_duration($age))
            . ' : la boucle seedbox est peut-être arrêtée.</div>';
    }
    if (empty($seedbox['ok'])) {
        echo '<div class="notice error">Deluge injoignable : ' . h(isset($seedbox['error']) ? $seedbox['error'] : '') . '</div>';
    }
    if (!empty($seedbox['queue_pending'])) {
        echo '<p>' . (int)$seedbox['queue_pending'] . " torrent(s) en attente d'ajout dans Deluge…</p>";
    }

    $torrents = isset($seedbox['torrents']) ? $seedbox['torrents'] : array();
    if (!$torrents) {
        echo '<p class="muted">Aucun téléchargement.</p>';
    }

    $piCurrent = isset($pi['current']) ? $pi['current'] : null;
    $piManifest = isset($pi['manifest']) ? $pi['manifest'] : null;

    foreach ($torrents as $t) {
        list($label, $class) = stage_label($t, $pi);
        $title = !empty($t['title']) ? $t['title'] : $t['name'];

        echo '<article class="item">';
        echo '<div class="title">' . h($title) . '</div>';
        if (!empty($t['title']) && $t['title'] !== $t['name']) {
            echo '<div class="sub">' . h($t['name']) . '</div>';
        }
        echo '<div class="stage ' . h($class) . '">' . h($label);
        if (!empty($t['detail'])) {
            echo ' — ' . h($t['detail']);
        }
        echo '</div>';

        if ($t['stage'] === 'downloading') {
            echo progress_bar($t['progress']);
            $bits = array(sprintf('%.1f %%', $t['progress']), fmt_bytes($t['done']) . ' / ' . fmt_bytes($t['size']));
            if ($t['down_rate'] > 0) {
                $bits[] = '↓ ' . fmt_speed($t['down_rate']);
            }
            if ($t['eta'] > 0) {
                $bits[] = 'reste ' . fmt_duration($t['eta']);
            }
            $bits[] = $t['seeds'] . ' sources';
            echo '<div class="meta">' . h(implode(' · ', $bits)) . '</div>';
        } elseif ($t['stage'] === 'ready_for_pi' && $piManifest && $piManifest['torrent_id'] === $t['id']) {
            $total = 0;
            $done = 0;
            foreach ($piManifest['files'] as $f) {
                $total += $f['size'];
                if ($f['state'] === 'done' || $f['state'] === 'present') {
                    $done += $f['size'];
                }
            }
            if ($piCurrent) {
                $done += $piCurrent['done'];
            }
            $percent = $total > 0 ? 100 * $done / $total : 0;
            echo progress_bar($percent, 'pi');
            echo '<div class="meta">' . h(sprintf('%.1f %%', $percent) . ' · ' . fmt_bytes($done) . ' / ' . fmt_bytes($total)) . '</div>';
        } else {
            echo '<div class="meta">' . h(fmt_bytes($t['size']) . ' · ratio ' . $t['ratio']) . '</div>';
        }
        if ($t['stage'] !== 'removing') {
            if ($t['stage'] === 'on_pi') {
                $warning = 'Supprimer « ' . $title . ' » et ses fichiers de la seedbox ?' . "\n\n" . 'La copie sur la Raspberry Pi est conservée.';
            } elseif ($t['stage'] === 'ready_for_pi' && pi_is_transferring($pi, $t['id'])) {
                $warning = null;
            } else {
                $warning = 'Supprimer « ' . $title . ' » et ses fichiers de la seedbox ?' . "\n\n" . 'Il n\'est pas encore sur la Raspberry Pi : il ne sera jamais transféré.';
            }

            if ($warning === null) {
                echo '<div class="meta">Suppression possible une fois le transfert vers la Pi terminé.</div>';
            } else {
                echo '<form method="post" class="remove" onsubmit="return confirm(this.dataset.warning);" data-warning="' . h($warning) . '">'
                    . '<input type="hidden" name="csrf" value="' . h($csrf) . '">'
                    . '<input type="hidden" name="action" value="remove">'
                    . '<input type="hidden" name="torrent_id" value="' . h($t['id']) . '">'
                    . '<button type="submit" class="danger">Supprimer de la seedbox</button></form>';
            }
        }
        echo '</article>';
    }

    echo '</section>';
    return ob_get_clean();
}

function pi_is_transferring($pi, $torrentId)
{
    return $pi && isset($pi['updated_at'], $pi['manifest']['torrent_id'])
        && time() - $pi['updated_at'] < PI_STALE_AFTER
        && $pi['manifest']['torrent_id'] === $torrentId;
}

function render_pi($pi)
{
    ob_start();
    echo '<section><h2>Raspberry Pi</h2>';

    if ($pi === null) {
        echo "<p class=\"muted\">La Raspberry Pi ne s'est pas encore manifestée.</p></section>";
        return ob_get_clean();
    }

    $age = time() - (int)$pi['updated_at'];
    $stale = $age > PI_STALE_AFTER;

    $states = array(
        'idle' => array('En veille', 'good'),
        'transferring' => array('Transfert en cours', 'active'),
        'error' => array('Erreur', 'bad'),
        'starting' => array('Démarrage', 'muted'),
    );
    $state = isset($states[$pi['state']]) ? $states[$pi['state']] : array($pi['state'], 'muted');
    if ($stale) {
        $state = array('Hors ligne ?', 'bad');
    }

    echo '<div class="stage ' . h($state[1]) . '">' . h($state[0]);
    if (!empty($pi['message'])) {
        echo ' — ' . h($pi['message']);
    }
    echo '</div>';

    $meta = array('Vue ' . fmt_ago($pi['updated_at']));
    if (!empty($pi['disk'])) {
        $meta[] = fmt_bytes($pi['disk']['free']) . ' libres sur ' . fmt_bytes($pi['disk']['total']);
    }
    echo '<div class="meta">' . h(implode(' · ', $meta)) . '</div>';

    if (!$stale && !empty($pi['manifest'])) {
        $m = $pi['manifest'];
        $c = isset($pi['current']) ? $pi['current'] : null;

        echo '<article class="item"><div class="title">' . h($m['name']) . '</div>';

        if ($c) {
            $percent = $c['size'] > 0 ? 100 * $c['done'] / $c['size'] : 100;
            echo '<div class="sub">' . h(basename($c['target'])) . '</div>';
            echo progress_bar($percent, 'pi');
            $bits = array(sprintf('%.1f %%', $percent), fmt_bytes($c['done']) . ' / ' . fmt_bytes($c['size']));
            if (!empty($c['speed'])) {
                $bits[] = '↓ ' . fmt_speed($c['speed']);
            }
            if (!empty($c['eta'])) {
                $bits[] = 'reste ' . fmt_duration($c['eta']);
            }
            echo '<div class="meta">' . h(implode(' · ', $bits)) . '</div>';
            if (!empty($c['playable'])) {
                echo '<div class="stage good">▶ Déjà visible dans Kodi, lecture possible</div>';
            }
        }

        $icons = array('waiting' => '…', 'downloading' => '↓', 'done' => '✓', 'present' => '✓', 'error' => '✗');
        echo '<ul class="files">';
        foreach ($m['files'] as $f) {
            $icon = isset($icons[$f['state']]) ? $icons[$f['state']] : '?';
            echo '<li class="' . h($f['state']) . '"><span>' . h($icon) . '</span> '
                . h(basename($f['target'])) . ' <small>' . h(fmt_bytes($f['size'])) . '</small></li>';
        }
        echo '</ul></article>';
    }

    if (!empty($pi['errors'])) {
        echo '<details><summary>Dernières erreurs (' . count($pi['errors']) . ')</summary><ul class="files">';
        foreach (array_slice($pi['errors'], 0, 5) as $e) {
            echo '<li class="error"><small>' . h(date('d/m H:i', $e['at'])) . '</small> ' . h($e['message']) . '</li>';
        }
        echo '</ul></details>';
    }

    if (!empty($pi['recent'])) {
        echo '<details><summary>Derniers transferts</summary><ul class="files">';
        foreach ($pi['recent'] as $r) {
            echo '<li class="' . ($r['ok'] ? 'done' : 'error') . '"><span>' . ($r['ok'] ? '✓' : '✗') . '</span> '
                . h($r['name']) . ' <small>' . h(fmt_ago($r['finished_at'])) . '</small></li>';
        }
        echo '</ul></details>';
    }

    echo '</section>';
    return ob_get_clean();
}

function render_status($statusDir, $csrf)
{
    $seedbox = read_json_file($statusDir . '/seedbox.json');
    $pi = read_json_file($statusDir . '/pi.json');
    return render_seedbox($seedbox, $pi, $csrf) . render_pi($pi)
        . '<p class="updated"><small>Actualisé à ' . h(date('H:i:s')) . '</small></p>';
}

/* ------------------------------------------------- fragment rafraichi en JS */

if (isset($_GET['fragment']) && $_GET['fragment'] === 'status') {
    $authenticated = !empty($_SESSION['authenticated']);
    $csrf = $_SESSION['csrf'];
    session_write_close();
    header('Cache-Control: no-store');
    if (!$authenticated) {
        http_response_code(403);
        exit;
    }
    header('Content-Type: text/html; charset=UTF-8');
    echo render_status($statusDir, $csrf);
    exit;
}

/* ------------------------------------------------------------- formulaires */

$message = '';
$error = '';

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $token = isset($_POST['csrf']) ? $_POST['csrf'] : '';

    if (!is_string($token) || !hash_equals($_SESSION['csrf'], $token)) {
        $error = 'Session invalide. Recharge la page.';
    } else {
        $action = isset($_POST['action']) ? $_POST['action'] : '';

        if ($action === 'login') {
            $password = isset($_POST['password']) ? (string)$_POST['password'] : '';
            $locked = login_locked_for($stateDir);

            if ($locked > 0) {
                $error = 'Trop de tentatives. Réessaie dans ' . fmt_duration($locked) . '.';
            } elseif (password_verify($password, $config['password_hash'])) {
                clear_login_failures($stateDir);
                session_regenerate_id(true);
                $_SESSION['authenticated'] = true;
                $_SESSION['csrf'] = bin2hex(random_bytes(32));
                $message = 'Connexion réussie.';
            } else {
                register_login_failure($stateDir);
                sleep(1);
                $error = 'Mot de passe incorrect.';
            }
        } elseif ($action === 'logout') {
            $_SESSION = array();
            session_destroy();
            header('Location: index.php');
            exit;
        } elseif ($action === 'remove') {
            $torrentId = isset($_POST['torrent_id']) ? strtolower((string)$_POST['torrent_id']) : '';
            $seedbox = read_json_file($statusDir . '/seedbox.json');
            $pi = read_json_file($statusDir . '/pi.json');
            $found = null;
            if ($seedbox && !empty($seedbox['torrents'])) {
                foreach ($seedbox['torrents'] as $t) {
                    if ($t['id'] === $torrentId) {
                        $found = $t;
                    }
                }
            }

            if (empty($_SESSION['authenticated'])) {
                $error = 'Tu dois te connecter.';
            } elseif (!preg_match('/^[0-9a-f]{40}$/', $torrentId) || $found === null) {
                $error = 'Torrent inconnu.';
            } elseif (pi_is_transferring($pi, $torrentId)) {
                $error = 'La Raspberry Pi est en train de récupérer ce torrent : réessaie une fois le transfert terminé.';
            } else {
                if (!is_dir($commandsDir)) {
                    @mkdir($commandsDir, 0700, true);
                }
                $request = json_encode(array('requested_at' => time(), 'name' => $found['name']));
                $temporary = $commandsDir . '/.remove-' . bin2hex(random_bytes(8));
                if (@file_put_contents($temporary, $request) !== false
                    && @rename($temporary, $commandsDir . '/' . $torrentId . '.remove')) {
                    $label = !empty($found['title']) ? $found['title'] : $found['name'];
                    $message = 'Suppression de « ' . $label . ' » demandée : elle sera faite dans moins d’une minute.';
                } else {
                    @unlink($temporary);
                    $error = 'Impossible d’enregistrer la demande de suppression.';
                }
            }
        } elseif ($action === 'upload') {
            if (empty($_SESSION['authenticated'])) {
                $error = 'Tu dois te connecter.';
            } elseif (!isset($_FILES['torrent']) || is_array($_FILES['torrent']['name'])) {
                $error = 'Aucun fichier reçu.';
            } else {
                $file = $_FILES['torrent'];

                if ($file['error'] !== UPLOAD_ERR_OK) {
                    $error = 'Erreur pendant le transfert (code ' . (int)$file['error'] . ').';
                } elseif ($file['size'] <= 0 || $file['size'] > $config['max_upload_bytes']) {
                    $error = 'Fichier vide ou supérieur à la limite de 50 Mo.';
                } elseif (strtolower(pathinfo($file['name'], PATHINFO_EXTENSION)) !== 'torrent') {
                    $error = 'Seuls les fichiers .torrent sont acceptés.';
                } else {
                    $content = file_get_contents($file['tmp_name']);

                    if ($content === false || substr($content, 0, 1) !== 'd' || strpos($content, '4:info') === false) {
                        $error = 'Le fichier ne semble pas être un fichier torrent valide.';
                    } else {
                        $filename = bin2hex(random_bytes(16)) . '.torrent';
                        $destination = $config['queue_dir'] . '/' . $filename;

                        if (move_uploaded_file($file['tmp_name'], $destination)) {
                            chmod($destination, 0600);
                            $message = 'Fichier reçu et placé dans la file d’attente.';
                        } else {
                            $error = 'Impossible d’enregistrer le fichier.';
                        }
                    }
                }
            }
        }
    }
}

$authenticated = !empty($_SESSION['authenticated']);
?>
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Seedbox Manager</title>
<style>
body {
    font-family: Arial, sans-serif;
    background: #15191f;
    color: #f2f4f7;
    margin: 0;
    padding: 24px 16px;
}
main {
    max-width: 560px;
    margin: 24px auto;
    background: #222832;
    padding: 24px;
    border-radius: 14px;
}
h1 { margin-top: 0; }
h2 { margin: 28px 0 12px; font-size: 20px; }
p { line-height: 1.5; color: #c5cbd3; }
input, button {
    box-sizing: border-box;
    width: 100%;
    padding: 13px;
    margin-top: 12px;
    border-radius: 8px;
    font-size: 16px;
}
input {
    background: #15191f;
    color: white;
    border: 1px solid #505967;
}
button {
    background: #3278d3;
    color: white;
    border: 0;
    font-weight: bold;
}
button.secondary { background: #505967; }
form.remove button {
    width: auto;
    padding: 7px 12px;
    margin-top: 10px;
    font-size: 13px;
}
button.danger { background: #8a3434; }
.notice {
    padding: 12px;
    border-radius: 8px;
    margin: 16px 0;
    background: #244b37;
}
.error { background: #633333; }
small, .muted, .meta, .sub { color: #aab2bd; }
.item {
    background: #1a1f27;
    border-radius: 10px;
    padding: 12px 14px;
    margin: 10px 0;
}
.title { font-weight: bold; word-break: break-word; }
.sub { font-size: 13px; word-break: break-all; margin-top: 2px; }
.meta { font-size: 13px; margin-top: 6px; }
.stage { font-size: 14px; margin-top: 6px; }
.stage.active { color: #6fb1ff; }
.stage.good { color: #6fd39a; }
.stage.bad { color: #ff8a8a; }
.stage.muted { color: #aab2bd; }
.bar {
    height: 8px;
    background: #323a46;
    border-radius: 4px;
    overflow: hidden;
    margin-top: 8px;
}
.bar span { display: block; height: 100%; background: #3278d3; transition: width .5s; }
.bar.pi span { background: #3fae6f; }
.files { list-style: none; padding: 0; margin: 10px 0 0; font-size: 13px; }
.files li { padding: 3px 0; word-break: break-all; color: #c5cbd3; }
.files li span { display: inline-block; width: 16px; }
.files li.done span, .files li.present span { color: #6fd39a; }
.files li.downloading { color: #6fb1ff; }
.files li.error { color: #ff8a8a; background: none; }
details { margin-top: 12px; }
summary { cursor: pointer; color: #c5cbd3; }
.updated { text-align: right; margin: 8px 0 0; }
</style>
</head>
<body>
<main>
<h1>Seedbox Manager</h1>
<p>Gestion privée de tes téléchargements.</p>

<?php if ($message !== ''): ?>
<div class="notice"><?php echo h($message); ?></div>
<?php endif; ?>

<?php if ($error !== ''): ?>
<div class="notice error"><?php echo h($error); ?></div>
<?php endif; ?>

<?php if (!$authenticated): ?>
<h2>Connexion</h2>
<form method="post">
    <input type="hidden" name="csrf" value="<?php echo h($_SESSION['csrf']); ?>">
    <input type="hidden" name="action" value="login">
    <input type="password" name="password" placeholder="Mot de passe" required autocomplete="current-password">
    <button type="submit">Se connecter</button>
</form>
<?php else: ?>
<h2>Ajouter un torrent</h2>
<form method="post" enctype="multipart/form-data">
    <input type="hidden" name="csrf" value="<?php echo h($_SESSION['csrf']); ?>">
    <input type="hidden" name="action" value="upload">
    <input type="file" name="torrent" accept=".torrent" required>
    <button type="submit">Envoyer le torrent</button>
</form>
<p><small>Format accepté : .torrent — taille maximale : 50 Mo.</small></p>

<div id="status"><?php echo render_status($statusDir, $_SESSION['csrf']); ?></div>

<form method="post">
    <input type="hidden" name="csrf" value="<?php echo h($_SESSION['csrf']); ?>">
    <input type="hidden" name="action" value="logout">
    <button class="secondary" type="submit">Se déconnecter</button>
</form>

<script>
(function () {
    var box = document.getElementById('status');
    function refresh() {
        if (document.hidden) { return; }
        fetch('index.php?fragment=status', { credentials: 'same-origin', cache: 'no-store' })
            .then(function (r) {
                if (r.status === 403) { location.reload(); throw new Error('session'); }
                return r.ok ? r.text() : Promise.reject(r.status);
            })
            .then(function (html) {
                var open = Array.prototype.map.call(box.querySelectorAll('details'), function (d) { return d.open; });
                box.innerHTML = html;
                Array.prototype.forEach.call(box.querySelectorAll('details'), function (d, i) { d.open = !!open[i]; });
            })
            .catch(function () {});
    }
    setInterval(refresh, 10000);
    document.addEventListener('visibilitychange', refresh);
})();
</script>
<?php endif; ?>
</main>
</body>
</html>
