// user_profile.dart
import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:frontend/chats/batik_theme.dart';
import 'package:frontend/services/token_storage.dart';
import 'package:go_router/go_router.dart';
import 'package:google_fonts/google_fonts.dart';

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

final _dio = Dio(
  BaseOptions(
    baseUrl: 'http://localhost:8000',
    connectTimeout: const Duration(seconds: 10),
    receiveTimeout: const Duration(seconds: 10),
    headers: {'Content-Type': 'application/json'},
  ),
);

// ---------------------------------------------------------------------------
// Warna & konstanta — dipetakan dari palet batik
// ---------------------------------------------------------------------------

const _bg         = BatikColors.kertas;
const _neon       = BatikColors.marun;   // aksen utama (pengganti neon hijau)
const _neonDim    = Color(0x268B251E);   // marun 15%
const _surface    = BatikColors.putih;
const _surfaceAlt = BatikColors.krem;
const _textMuted  = BatikColors.abuTeks;

// Interval refresh token — 25 menit agar aman sebelum server expire (30 menit)
const _kRefreshInterval = Duration(minutes: 25);

// ---------------------------------------------------------------------------
// Model sederhana
// ---------------------------------------------------------------------------

class _UserData {
  final int id;
  final String username;
  final String email;
  final String name;
  final bool isVerified;
  final bool isActive;

  const _UserData({
    required this.id,
    required this.username,
    required this.email,
    required this.name,
    required this.isVerified,
    required this.isActive,
  });

  factory _UserData.fromJson(Map<String, dynamic> json) => _UserData(
    id: json['id'] as int,
    username: json['username'] as String,
    email: json['email'] as String,
    name: json['name'] as String,
    isVerified: json['is_verified'] as bool,
    isActive: json['is_active'] as bool,
  );
}

class _SessionItem {
  final int sessionId;
  final String deviceInfo;
  final String createdAt;
  final String accessTokenExpiresAt;
  final bool isCurrent;

  const _SessionItem({
    required this.sessionId,
    required this.deviceInfo,
    required this.createdAt,
    required this.accessTokenExpiresAt,
    required this.isCurrent,
  });

  factory _SessionItem.fromJson(Map<String, dynamic> json) => _SessionItem(
    sessionId: json['session_id'] as int,
    deviceInfo: json['device_info'] as String? ?? 'Unknown Device',
    createdAt: json['created_at'] as String,
    accessTokenExpiresAt: json['access_token_expires_at'] as String,
    isCurrent: json['is_current'] as bool,
  );
}

// ---------------------------------------------------------------------------
// User Profile Page
// ---------------------------------------------------------------------------

class UserProfilePage extends StatefulWidget {
  const UserProfilePage({super.key});

  @override
  State<UserProfilePage> createState() => _UserProfilePageState();
}

class _UserProfilePageState extends State<UserProfilePage>
    with SingleTickerProviderStateMixin {
  // ── Auth state — dibaca dari token_storage ───────────────────────────────
  String? _accessToken;
  int? _userId;
  Timer? _refreshTimer;

  _UserData? _user;
  List<_SessionItem> _sessions = [];
  bool _loadingUser = true;
  bool _loadingLogout = false;
  String? _error;

  late final AnimationController _fadeController;
  late final Animation<double> _fadeAnimation;

  @override
  void initState() {
    super.initState();
    _fadeController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 700),
    );
    _fadeAnimation = CurvedAnimation(
      parent: _fadeController,
      curve: Curves.easeOut,
    );
    _initAuth();
  }

  Future<void> _initAuth() async {
    try {
      _accessToken = await TokenStorage.read(key: 'access_token');
      _userId = await TokenStorage.getUserId();
    } catch (_) {}

    if (_accessToken == null || _accessToken!.isEmpty || _userId == null) {
      if (mounted) context.go('/users/login');
      return;
    }

    _startRefreshTimer();
    _fetchData();
  }

  @override
  void dispose() {
    _refreshTimer?.cancel();
    _fadeController.dispose();
    super.dispose();
  }

  // ── Background token refresh ───────────────────────────────────────────────

  void _startRefreshTimer() {
    _refreshTimer?.cancel();
    _refreshTimer = Timer.periodic(_kRefreshInterval, (_) => _silentRefresh());
  }

  Future<void> _silentRefresh() async {
    final currentRefresh = await TokenStorage.read(key: 'refresh_token');
    if (currentRefresh == null || currentRefresh.isEmpty) {
      _forceLogout();
      return;
    }

    try {
      final response = await _dio.post(
        '/users/refresh-token',
        data: {'refresh_token': currentRefresh},
      );

      if (response.statusCode == 200) {
        final data = response.data['data'] as Map<String, dynamic>;
        final newAccess = data['access_token'] as String;
        final newRefresh = data['refresh_token'] as String;

        await Future.wait([
          TokenStorage.write(key: 'access_token', value: newAccess),
          TokenStorage.write(key: 'refresh_token', value: newRefresh),
        ]);

        if (data['created_at'] != null) {
          await TokenStorage.write(
            key: 'session_created_at',
            value: data['created_at'] as String,
          );
        }

        if (mounted) setState(() => _accessToken = newAccess);
      }
    } on DioException catch (e) {
      final statusCode = e.response?.statusCode;
      if (statusCode == 401 || statusCode == 403) {
        _forceLogout();
      }
    } catch (_) {}
  }

  Future<void> _forceLogout() async {
    _refreshTimer?.cancel();
    await TokenStorage.deleteAll();
    if (mounted) context.go('/users/login');
  }

  Map<String, String> get _authHeaders => {
    'Authorization': 'Bearer ${_accessToken ?? ''}',
  };

  // ── Fetch user + sessions sekaligus ───────────────────────────────────────

  Future<void> _fetchData() async {
    setState(() {
      _loadingUser = true;
      _error = null;
    });

    try {
      final results = await Future.wait([
        _dio.get('/users/$_userId', options: Options(headers: _authHeaders)),
        _dio.get('/users/sessions', options: Options(headers: _authHeaders)),
      ]);

      final userData = results[0].data['data'] as Map<String, dynamic>;
      final sessionData = results[1].data['data'] as Map<String, dynamic>;
      final sessionsList = sessionData['sessions'] as List<dynamic>;

      setState(() {
        _user = _UserData.fromJson(userData);
        _sessions = sessionsList
            .map((s) => _SessionItem.fromJson(s as Map<String, dynamic>))
            .toList();
        _loadingUser = false;
      });

      _fadeController.forward(from: 0);
    } on DioException catch (e) {
      String msg = 'Gagal memuat data.';

      if (e.response?.statusCode == 401 || e.response?.statusCode == 403) {
        _forceLogout();
        return;
      }

      if (e.response?.data['detail'] != null) {
        msg = e.response!.data['detail'].toString();
      }
      setState(() {
        _error = msg;
        _loadingUser = false;
      });
    } catch (_) {
      setState(() {
        _error = 'Terjadi kesalahan tidak terduga.';
        _loadingUser = false;
      });
    }
  }

  // ── Logout device ini ─────────────────────────────────────────────────────

  Future<void> _handleLogout() async {
    final confirmed = await _showConfirmDialog(
      title: 'Keluar',
      message: 'Yakin ingin keluar dari akun ini?',
      confirm: 'Keluar',
    );
    if (!confirmed || !mounted) return;

    setState(() => _loadingLogout = true);

    try {
      await _dio.post('/users/logout', options: Options(headers: _authHeaders));
      await TokenStorage.deleteAll();

      if (mounted) context.go('/users/login');
    } on DioException catch (e) {
      if (!mounted) return;
      String msg = 'Gagal logout.';
      if (e.response?.data['detail'] != null) {
        msg = e.response!.data['detail'].toString();
      }
      _showErrorSnackbar(msg);
    } finally {
      if (mounted) setState(() => _loadingLogout = false);
    }
  }

  // ── Logout other devices ──────────────────────────────────────────────────

  Future<void> _handleLogoutOtherDevices() async {
    final otherCount = _sessions.where((s) => !s.isCurrent).length;
    if (otherCount == 0) {
      _showInfoSnackbar('Tidak ada device lain yang aktif.');
      return;
    }

    final confirmed = await _showConfirmDialog(
      title: 'Logout Device Lain',
      message: 'Logout dari $otherCount device lain yang sedang aktif?',
      confirm: 'Logout Semua',
    );
    if (!confirmed || !mounted) return;

    try {
      await _dio.post(
        '/users/logout/other-devices',
        options: Options(headers: _authHeaders),
      );
      if (mounted) {
        _showSuccessSnackbar('Berhasil logout dari $otherCount device lain.');
        _fetchData();
      }
    } on DioException catch (e) {
      if (!mounted) return;
      String msg = 'Gagal logout device lain.';
      if (e.response?.data['detail'] != null) {
        msg = e.response!.data['detail'].toString();
      }
      _showErrorSnackbar(msg);
    }
  }

  // ── Dialog konfirmasi ─────────────────────────────────────────────────────

  Future<bool> _showConfirmDialog({
    required String title,
    required String message,
    required String confirm,
  }) async {
    final result = await showDialog<bool>(
      context: context,
      builder: (ctx) => Dialog(
        backgroundColor: BatikColors.putih,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(16),
          side: const BorderSide(color: BatikColors.garis),
        ),
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                title,
                style: GoogleFonts.playfairDisplay(
                  fontSize: 17,
                  fontWeight: FontWeight.w700,
                  color: BatikColors.sogaGelap,
                ),
              ),
              const SizedBox(height: 10),
              Text(
                message,
                style: GoogleFonts.nunitoSans(
                  fontSize: 13,
                  color: BatikColors.abuTeks,
                  height: 1.5,
                ),
              ),
              const SizedBox(height: 24),
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton(
                      onPressed: () => Navigator.of(ctx).pop(false),
                      style: OutlinedButton.styleFrom(
                        side: const BorderSide(color: BatikColors.garis),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(10),
                        ),
                        padding: const EdgeInsets.symmetric(vertical: 12),
                      ),
                      child: Text(
                        'Batal',
                        style: GoogleFonts.nunitoSans(
                          fontSize: 13,
                          color: BatikColors.abuTeks,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: ElevatedButton(
                      onPressed: () => Navigator.of(ctx).pop(true),
                      style: ElevatedButton.styleFrom(
                        backgroundColor: BatikColors.marun,
                        foregroundColor: Colors.white,
                        elevation: 0,
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(10),
                        ),
                        padding: const EdgeInsets.symmetric(vertical: 12),
                      ),
                      child: Text(
                        confirm,
                        style: GoogleFonts.nunitoSans(
                          fontSize: 13,
                          fontWeight: FontWeight.w600,
                          color: Colors.white,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
    return result ?? false;
  }

  // ── Snackbars ─────────────────────────────────────────────────────────────

  void _showErrorSnackbar(String message) {
    ScaffoldMessenger.of(context).showSnackBar(
      _buildSnackbar(message, BatikColors.marun),
    );
  }

  void _showSuccessSnackbar(String message) {
    ScaffoldMessenger.of(context).showSnackBar(
      _buildSnackbar(message, BatikColors.hijau),
    );
  }

  void _showInfoSnackbar(String message) {
    ScaffoldMessenger.of(context).showSnackBar(
      _buildSnackbar(message, BatikColors.soga),
    );
  }

  SnackBar _buildSnackbar(String message, Color borderColor) {
    return SnackBar(
      content: Text(
        message,
        style: GoogleFonts.nunitoSans(fontSize: 13, color: BatikColors.sogaGelap),
      ),
      backgroundColor: BatikColors.putih,
      behavior: SnackBarBehavior.floating,
      margin: const EdgeInsets.all(16),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(10),
        side: BorderSide(color: borderColor, width: 1),
      ),
    );
  }

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _bg,
      body: SafeArea(
        child: _loadingUser
            ? const Center(
                child: CircularProgressIndicator(
                  color: BatikColors.marun,
                  strokeWidth: 2,
                ),
              )
            : _error != null
            ? _ErrorView(error: _error!, onRetry: _fetchData)
            : FadeTransition(
                opacity: _fadeAnimation,
                child: Column(
                  children: [
                    // Tombol kembali
                    Padding(
                      padding: const EdgeInsets.fromLTRB(24, 16, 24, 8),
                      child: Align(
                        alignment: Alignment.centerLeft,
                        child: GestureDetector(
                          onTap: () => Navigator.of(context).pop(),
                          child: Container(
                            width: 40,
                            height: 40,
                            decoration: BoxDecoration(
                              color: BatikColors.marun.withValues(alpha: 0.12),
                              borderRadius: BorderRadius.circular(12),
                              boxShadow: [
                                BoxShadow(
                                  color: BatikColors.marun.withValues(alpha: 0.15),
                                  blurRadius: 12,
                                ),
                              ],
                            ),
                            child: const Icon(
                              Icons.arrow_back_rounded,
                              color: BatikColors.marun,
                              size: 24,
                            ),
                          ),
                        ),
                      ),
                    ),
                    Expanded(
                      child: CustomScrollView(
                        slivers: [
                          SliverPadding(
                            padding: const EdgeInsets.symmetric(
                              horizontal: 24,
                              vertical: 8,
                            ),
                            sliver: SliverList(
                              delegate: SliverChildListDelegate([
                                _ProfileCard(user: _user!),
                                const SizedBox(height: 24),
                                _CredentialCard(
                                  user: _user!,
                                  onChangeEmail: () => context
                                      .push(
                                        '/users/change-email/otp/verify-otp?email=${Uri.encodeComponent(_user!.email)}',
                                      )
                                      .then((_) => _fetchData()),
                                ),
                                const SizedBox(height: 24),
                                _SessionsCard(
                                  sessions: _sessions,
                                  onLogoutOtherDevices:
                                      _handleLogoutOtherDevices,
                                ),
                                const SizedBox(height: 32),
                                _LogoutButton(
                                  isLoading: _loadingLogout,
                                  onPressed: _handleLogout,
                                ),
                                const SizedBox(height: 32),
                              ]),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Profile Card
// ---------------------------------------------------------------------------

class _ProfileCard extends StatelessWidget {
  const _ProfileCard({required this.user});
  final _UserData user;

  String get _initials {
    final parts = user.name.trim().split(' ');
    if (parts.length >= 2) {
      return '${parts[0][0]}${parts[1][0]}'.toUpperCase();
    }
    return parts[0].substring(0, parts[0].length.clamp(0, 2)).toUpperCase();
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: _surface,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: BatikColors.garis),
        boxShadow: [
          BoxShadow(
            color: BatikColors.sogaGelap.withValues(alpha: 0.06),
            blurRadius: 24,
          ),
        ],
      ),
      child: Row(
        children: [
          Container(
            width: 56,
            height: 56,
            decoration: BoxDecoration(
              color: BatikColors.marun.withValues(alpha: 0.12),
              shape: BoxShape.circle,
              boxShadow: [
                BoxShadow(
                  color: BatikColors.marun.withValues(alpha: 0.18),
                  blurRadius: 18,
                  spreadRadius: 2,
                ),
              ],
            ),
            child: Center(
              child: Text(
                _initials,
                style: GoogleFonts.playfairDisplay(
                  fontSize: 22,
                  fontWeight: FontWeight.w700,
                  color: BatikColors.marun,
                ),
              ),
            ),
          ),
          const SizedBox(width: 16),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  user.name,
                  style: GoogleFonts.playfairDisplay(
                    fontSize: 17,
                    fontWeight: FontWeight.w700,
                    color: BatikColors.sogaGelap,
                    letterSpacing: -0.2,
                  ),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
                const SizedBox(height: 3),
                Text(
                  '@${user.username}',
                  style: GoogleFonts.nunitoSans(
                    fontSize: 13,
                    color: BatikColors.marun,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(height: 8),
                Row(
                  children: [
                    _StatusBadge(
                      label: user.isVerified
                          ? 'Terverifikasi'
                          : 'Belum Verifikasi',
                      color: user.isVerified
                          ? BatikColors.hijau
                          : BatikColors.emas,
                    ),
                    const SizedBox(width: 8),
                    _StatusBadge(
                      label: user.isActive ? 'Aktif' : 'Nonaktif',
                      color: user.isActive
                          ? BatikColors.hijau
                          : BatikColors.marun,
                    ),
                  ],
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _StatusBadge extends StatelessWidget {
  const _StatusBadge({required this.label, required this.color});
  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(6),
        border: Border.all(color: color.withValues(alpha: 0.35), width: 1),
      ),
      child: Text(
        label,
        style: GoogleFonts.nunitoSans(
          fontSize: 10,
          fontWeight: FontWeight.w600,
          color: color,
          letterSpacing: 0.2,
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Credential Card
// ---------------------------------------------------------------------------

class _CredentialCard extends StatelessWidget {
  const _CredentialCard({required this.user, this.onChangeEmail});
  final _UserData user;
  final VoidCallback? onChangeEmail;

  @override
  Widget build(BuildContext context) {
    return _SectionCard(
      title: 'Informasi Akun',
      icon: Icons.badge_outlined,
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 12),
          child: Row(
            children: [
              const Icon(
                Icons.mail_outline_rounded,
                size: 16,
                color: BatikColors.abuTeks,
              ),
              const SizedBox(width: 10),
              Text(
                'Email',
                style: GoogleFonts.nunitoSans(
                  fontSize: 12,
                  color: BatikColors.abuTeks,
                  fontWeight: FontWeight.w500,
                ),
              ),
              const Spacer(),
              Column(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  Text(
                    user.email,
                    style: GoogleFonts.nunitoSans(
                      fontSize: 13,
                      color: BatikColors.sogaGelap,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                  const SizedBox(height: 2),
                  GestureDetector(
                    onTap: onChangeEmail,
                    child: Text(
                      'Ganti Email',
                      style: GoogleFonts.nunitoSans(
                        fontSize: 11,
                        color: BatikColors.marun,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
        _Divider(),
        _InfoRow(
          icon: Icons.tag_rounded,
          label: 'User ID',
          value: '#${user.id}',
          valueColor: BatikColors.abuTeks,
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Sessions Card
// ---------------------------------------------------------------------------

class _SessionsCard extends StatelessWidget {
  const _SessionsCard({
    required this.sessions,
    required this.onLogoutOtherDevices,
  });

  final List<_SessionItem> sessions;
  final VoidCallback onLogoutOtherDevices;

  @override
  Widget build(BuildContext context) {
    final otherCount = sessions.where((s) => !s.isCurrent).length;

    return _SectionCard(
      title: 'Sesi Aktif',
      icon: Icons.devices_rounded,
      trailing: otherCount > 0
          ? GestureDetector(
              onTap: onLogoutOtherDevices,
              child: Text(
                'Logout device lain',
                style: GoogleFonts.nunitoSans(
                  fontSize: 11,
                  color: BatikColors.marun,
                  fontWeight: FontWeight.w600,
                ),
              ),
            )
          : null,
      children: sessions.isEmpty
          ? [
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 8),
                child: Text(
                  'Tidak ada sesi aktif.',
                  style: GoogleFonts.nunitoSans(
                    fontSize: 13,
                    color: BatikColors.abuTeks,
                  ),
                ),
              ),
            ]
          : [
              for (int i = 0; i < sessions.length; i++) ...[
                _SessionTile(session: sessions[i]),
                if (i < sessions.length - 1) _Divider(),
              ],
            ],
    );
  }
}

class _SessionTile extends StatelessWidget {
  const _SessionTile({required this.session});
  final _SessionItem session;

  String _formatDate(String iso) {
    try {
      final normalized = iso.endsWith('Z') ? iso : '${iso}Z';
      final dt = DateTime.parse(normalized).toLocal();
      final now = DateTime.now();
      final diff = now.difference(dt);
      if (diff.inMinutes < 1) return 'Baru saja';
      if (diff.inHours < 1) return '${diff.inMinutes} menit lalu';
      if (diff.inDays < 1) return '${diff.inHours} jam lalu';
      if (diff.inDays < 7) return '${diff.inDays} hari lalu';
      return '${dt.day}/${dt.month}/${dt.year}';
    } catch (_) {
      return iso;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 10),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 36,
            height: 36,
            decoration: BoxDecoration(
              color: session.isCurrent
                  ? BatikColors.marun.withValues(alpha: 0.12)
                  : BatikColors.krem,
              borderRadius: BorderRadius.circular(10),
            ),
            child: Icon(
              Icons.smartphone_rounded,
              size: 18,
              color: session.isCurrent
                  ? BatikColors.marun
                  : BatikColors.abuTeks,
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        session.deviceInfo,
                        style: GoogleFonts.nunitoSans(
                          fontSize: 12,
                          color: BatikColors.sogaGelap,
                          fontWeight: FontWeight.w600,
                        ),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                    if (session.isCurrent)
                      Container(
                        margin: const EdgeInsets.only(left: 8),
                        padding: const EdgeInsets.symmetric(
                          horizontal: 6,
                          vertical: 2,
                        ),
                        decoration: BoxDecoration(
                          color: BatikColors.marun.withValues(alpha: 0.12),
                          borderRadius: BorderRadius.circular(4),
                        ),
                        child: Text(
                          'Device ini',
                          style: GoogleFonts.nunitoSans(
                            fontSize: 9,
                            color: BatikColors.marun,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                  ],
                ),
                const SizedBox(height: 3),
                Text(
                  'Login ${_formatDate(session.createdAt)}',
                  style: GoogleFonts.nunitoSans(
                    fontSize: 11,
                    color: BatikColors.abuTeks,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Logout Button
// ---------------------------------------------------------------------------

class _LogoutButton extends StatelessWidget {
  const _LogoutButton({required this.isLoading, required this.onPressed});
  final bool isLoading;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: double.infinity,
      height: 52,
      child: OutlinedButton(
        onPressed: isLoading ? null : onPressed,
        style: OutlinedButton.styleFrom(
          side: const BorderSide(color: BatikColors.marun, width: 1.5),
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(14),
          ),
          backgroundColor: BatikColors.marun.withValues(alpha: 0.06),
        ),
        child: isLoading
            ? const SizedBox(
                width: 22,
                height: 22,
                child: CircularProgressIndicator(
                  strokeWidth: 2.5,
                  color: BatikColors.marun,
                ),
              )
            : Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(
                    Icons.logout_rounded,
                    size: 18,
                    color: BatikColors.marun,
                  ),
                  const SizedBox(width: 8),
                  Text(
                    'Keluar',
                    style: GoogleFonts.nunitoSans(
                      fontSize: 15,
                      fontWeight: FontWeight.w700,
                      color: BatikColors.marun,
                      letterSpacing: 0.2,
                    ),
                  ),
                ],
              ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Reusable section card
// ---------------------------------------------------------------------------

class _SectionCard extends StatelessWidget {
  const _SectionCard({
    required this.title,
    required this.icon,
    required this.children,
    this.trailing,
  });

  final String title;
  final IconData icon;
  final List<Widget> children;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: _surface,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: BatikColors.garis),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 0),
            child: Row(
              children: [
                Icon(icon, size: 15, color: BatikColors.marun),
                const SizedBox(width: 7),
                Text(
                  title,
                  style: GoogleFonts.nunitoSans(
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                    color: BatikColors.marun,
                    letterSpacing: 0.3,
                  ),
                ),
                if (trailing != null) ...[const Spacer(), trailing!],
              ],
            ),
          ),
          const SizedBox(height: 4),
          Container(height: 1, color: BatikColors.garis),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: children,
            ),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Info row
// ---------------------------------------------------------------------------

class _InfoRow extends StatelessWidget {
  const _InfoRow({
    required this.icon,
    required this.label,
    required this.value,
    this.valueColor,
  });

  final IconData icon;
  final String label;
  final String value;
  final Color? valueColor;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 12),
      child: Row(
        children: [
          Icon(icon, size: 16, color: BatikColors.abuTeks),
          const SizedBox(width: 10),
          Text(
            label,
            style: GoogleFonts.nunitoSans(
              fontSize: 12,
              color: BatikColors.abuTeks,
              fontWeight: FontWeight.w500,
            ),
          ),
          const Spacer(),
          Text(
            value,
            style: GoogleFonts.nunitoSans(
              fontSize: 13,
              color: valueColor ?? BatikColors.sogaGelap,
              fontWeight: FontWeight.w500,
            ),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Divider tipis
// ---------------------------------------------------------------------------

class _Divider extends StatelessWidget {
  @override
  Widget build(BuildContext context) =>
      Container(height: 1, color: BatikColors.garis);
}

// ---------------------------------------------------------------------------
// Error view
// ---------------------------------------------------------------------------

class _ErrorView extends StatelessWidget {
  const _ErrorView({required this.error, required this.onRetry});
  final String error;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.wifi_off_rounded,
                color: BatikColors.abuTeks, size: 48),
            const SizedBox(height: 16),
            Text(
              error,
              style: GoogleFonts.nunitoSans(
                fontSize: 13,
                color: BatikColors.abuTeks,
                height: 1.5,
              ),
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 24),
            OutlinedButton(
              onPressed: onRetry,
              style: OutlinedButton.styleFrom(
                side: const BorderSide(color: BatikColors.marun),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(10),
                ),
                padding: const EdgeInsets.symmetric(
                  horizontal: 28,
                  vertical: 12,
                ),
              ),
              child: Text(
                'Coba Lagi',
                style: GoogleFonts.nunitoSans(
                  fontSize: 13,
                  color: BatikColors.marun,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}