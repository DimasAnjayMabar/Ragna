import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:frontend/chats/service_chats.dart';
import 'package:frontend/chats/batik_theme.dart';
import 'package:google_fonts/google_fonts.dart';

// ---------------------------------------------------------------------------
// Konstanta Warna — dipetakan dari palet batik
// ---------------------------------------------------------------------------

const kChatBg         = BatikColors.kertas;
const kChatNeon       = BatikColors.marun;        // aksen utama
const kChatNeonDim    = Color(0x268B251E);        // marun 15%
const kChatSurface    = BatikColors.putih;
const kChatSurfaceAlt = BatikColors.krem;
const kChatBorder     = BatikColors.garis;
const kChatTextMuted  = BatikColors.abuTeks;
const kSidebarWidth   = 280.0;

// ---------------------------------------------------------------------------
// Sidebar (root widget yang di-export)
// ---------------------------------------------------------------------------

class ChatSidebar extends StatelessWidget {
  const ChatSidebar({
    super.key,
    required this.topics,
    required this.loading,
    required this.activeChatId,
    required this.profile,
    required this.renamingId,
    required this.renamingTemp,
    required this.onNewChat,
    required this.onSelectTopic,
    required this.onDeleteTopic,
    required this.onStartRename,
    required this.onConfirmRename,
    required this.onCancelRename,
    required this.onRenameChange,
    required this.onProfileTap,
    required this.onLogout,
  });

  final List<ChatTopic>  topics;
  final bool              loading;
  final int?              activeChatId;
  final ChatUserProfile?  profile;
  final int?              renamingId;
  final String?           renamingTemp;
  final VoidCallback      onNewChat;
  final void Function(ChatTopic) onSelectTopic;
  final void Function(ChatTopic) onDeleteTopic;
  final void Function(ChatTopic) onStartRename;
  final void Function(ChatTopic, String) onConfirmRename;
  final VoidCallback      onCancelRename;
  final void Function(String) onRenameChange;
  final VoidCallback      onProfileTap;
  final VoidCallback      onLogout;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: kSidebarWidth,
      decoration: const BoxDecoration(
        color: kChatSurface,
        border: Border(right: BorderSide(color: kChatBorder)),
      ),
      child: Column(
        children: [
          // ── Logo + New Chat ───────────────────────────────────────────────
          _SidebarLogo(onNewChat: onNewChat),
          Container(height: 1, color: kChatBorder),

          // ── Topics List ───────────────────────────────────────────────────
          Expanded(child: _TopicList(
            topics      : topics,
            loading     : loading,
            activeChatId: activeChatId,
            renamingId  : renamingId,
            renamingTemp: renamingTemp,
            onSelect    : onSelectTopic,
            onDelete    : onDeleteTopic,
            onStartRename   : onStartRename,
            onConfirmRename : onConfirmRename,
            onCancelRename  : onCancelRename,
            onRenameChange  : onRenameChange,
          )),

          Container(height: 1, color: kChatBorder),

          // ── Profile Card ──────────────────────────────────────────────────
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 8),
            child: _ProfileCard(profile: profile, onTap: onProfileTap),
          ),

          // ── Logout ────────────────────────────────────────────────────────
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 0, 12, 16),
            child: _LogoutButton(onPressed: () => _showLogoutConfirmDialog(context, onLogout)),
          ),
        ],
      ),
    );
  }

  void _showLogoutConfirmDialog(BuildContext context, VoidCallback onLogout) {
    showDialog(
      context: context,
      builder: (BuildContext context) {
        return Dialog(
          backgroundColor: BatikColors.putih,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
            side: const BorderSide(color: BatikColors.garis),
          ),
          child: Container(
            padding: const EdgeInsets.all(20),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: BatikColors.marun.withValues(alpha: 0.1),
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(
                    Icons.logout_rounded,
                    size: 32,
                    color: BatikColors.marun,
                  ),
                ),
                const SizedBox(height: 16),
                Text(
                  'Konfirmasi Keluar',
                  style: GoogleFonts.playfairDisplay(
                    fontSize: 18,
                    fontWeight: FontWeight.w700,
                    color: BatikColors.sogaGelap,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  'Apakah Anda yakin ingin keluar dari aplikasi?',
                  textAlign: TextAlign.center,
                  style: GoogleFonts.nunitoSans(
                    fontSize: 13,
                    color: BatikColors.abuTeks,
                  ),
                ),
                const SizedBox(height: 24),
                Row(
                  children: [
                    Expanded(
                      child: OutlinedButton(
                        onPressed: () => Navigator.pop(context),
                        style: OutlinedButton.styleFrom(
                          side: const BorderSide(color: BatikColors.garis),
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(10),
                          ),
                          padding: const EdgeInsets.symmetric(vertical: 10),
                        ),
                        child: Text(
                          'Batal',
                          style: GoogleFonts.nunitoSans(
                            fontSize: 13,
                            fontWeight: FontWeight.w600,
                            color: BatikColors.abuTeks,
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: ElevatedButton(
                        onPressed: () {
                          Navigator.pop(context);
                          onLogout();
                        },
                        style: ElevatedButton.styleFrom(
                          backgroundColor: BatikColors.marun,
                          foregroundColor: Colors.white,
                          elevation: 0,
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(10),
                          ),
                          padding: const EdgeInsets.symmetric(vertical: 10),
                        ),
                        child: Text(
                          'Keluar',
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
        );
      },
    );
  }
}

// ---------------------------------------------------------------------------
// Logo + New Chat Button
// ---------------------------------------------------------------------------

class _SidebarLogo extends StatelessWidget {
  const _SidebarLogo({required this.onNewChat});
  final VoidCallback onNewChat;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 20, 12, 16),
      child: Row(
        children: [
          Container(
            width: 32,
            height: 32,
            decoration: BoxDecoration(
              color: BatikColors.marun.withValues(alpha: 0.12),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: BatikColors.marun.withValues(alpha: 0.4)),
            ),
            child: const Icon(
              Icons.account_tree_rounded,
              color: BatikColors.marun,
              size: 18,
            ),
          ),
          const SizedBox(width: 10),
          Text(
            'Ragna',
            style: GoogleFonts.playfairDisplay(
              fontSize: 17,
              fontWeight: FontWeight.w700,
              color: BatikColors.sogaGelap,
              letterSpacing: 0.3,
            ),
          ),
          const Spacer(),
          Tooltip(
            message: 'Chat Baru',
            child: InkWell(
              onTap: onNewChat,
              borderRadius: BorderRadius.circular(8),
              child: Container(
                padding: const EdgeInsets.all(6),
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: BatikColors.garis),
                ),
                child: const Icon(
                  Icons.edit_square,
                  size: 16,
                  color: BatikColors.abuTeks,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Topic List
// ---------------------------------------------------------------------------

class _TopicList extends StatelessWidget {
  const _TopicList({
    required this.topics,
    required this.loading,
    required this.activeChatId,
    required this.renamingId,
    required this.renamingTemp,
    required this.onSelect,
    required this.onDelete,
    required this.onStartRename,
    required this.onConfirmRename,
    required this.onCancelRename,
    required this.onRenameChange,
  });

  final List<ChatTopic> topics;
  final bool            loading;
  final int?            activeChatId;
  final int?            renamingId;
  final String?         renamingTemp;
  final void Function(ChatTopic) onSelect;
  final void Function(ChatTopic) onDelete;
  final void Function(ChatTopic) onStartRename;
  final void Function(ChatTopic, String) onConfirmRename;
  final VoidCallback    onCancelRename;
  final void Function(String) onRenameChange;

  @override
  Widget build(BuildContext context) {
    if (loading) {
      return const Center(
        child: CircularProgressIndicator(color: BatikColors.marun, strokeWidth: 2),
      );
    }
    if (topics.isEmpty) {
      return Center(
        child: Text(
          'Belum ada percakapan.',
          style: GoogleFonts.nunitoSans(fontSize: 12, color: BatikColors.abuTeks),
        ),
      );
    }
    return ListView.builder(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      itemCount: topics.length,
      itemBuilder: (_, i) {
        final t = topics[i];
        return _TopicTile(
          topic          : t,
          isActive       : t.id == activeChatId,
          isRenaming     : t.id == renamingId,
          renamingTemp   : renamingTemp,
          onTap          : () => onSelect(t),
          onDelete       : () => _showDeleteConfirmDialog(context, t, onDelete),
          onStartRename  : () => onStartRename(t),
          onConfirmRename: (v) => onConfirmRename(t, v),
          onCancelRename : onCancelRename,
          onRenameChange : onRenameChange,
        );
      },
    );
  }

  void _showDeleteConfirmDialog(BuildContext context, ChatTopic topic, void Function(ChatTopic) onDelete) {
    showDialog(
      context: context,
      builder: (BuildContext context) {
        return Dialog(
          backgroundColor: BatikColors.putih,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
            side: const BorderSide(color: BatikColors.garis),
          ),
          child: Container(
            padding: const EdgeInsets.all(20),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    color: BatikColors.marun.withValues(alpha: 0.1),
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(
                    Icons.delete_outline_rounded,
                    size: 32,
                    color: BatikColors.marun,
                  ),
                ),
                const SizedBox(height: 16),
                Text(
                  'Hapus Percakapan',
                  style: GoogleFonts.playfairDisplay(
                    fontSize: 18,
                    fontWeight: FontWeight.w700,
                    color: BatikColors.sogaGelap,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  'Apakah Anda yakin ingin menghapus "${topic.title}"?',
                  textAlign: TextAlign.center,
                  style: GoogleFonts.nunitoSans(
                    fontSize: 13,
                    color: BatikColors.abuTeks,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  'Percakapan ini akan dihapus secara permanen.',
                  textAlign: TextAlign.center,
                  style: GoogleFonts.nunitoSans(
                    fontSize: 11,
                    color: BatikColors.abuTeks,
                  ),
                ),
                const SizedBox(height: 24),
                Row(
                  children: [
                    Expanded(
                      child: OutlinedButton(
                        onPressed: () => Navigator.pop(context),
                        style: OutlinedButton.styleFrom(
                          side: const BorderSide(color: BatikColors.garis),
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(10),
                          ),
                          padding: const EdgeInsets.symmetric(vertical: 10),
                        ),
                        child: Text(
                          'Batal',
                          style: GoogleFonts.nunitoSans(
                            fontSize: 13,
                            fontWeight: FontWeight.w600,
                            color: BatikColors.abuTeks,
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: ElevatedButton(
                        onPressed: () {
                          Navigator.pop(context);
                          onDelete(topic);
                        },
                        style: ElevatedButton.styleFrom(
                          backgroundColor: BatikColors.marun,
                          foregroundColor: Colors.white,
                          elevation: 0,
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(10),
                          ),
                          padding: const EdgeInsets.symmetric(vertical: 10),
                        ),
                        child: Text(
                          'Hapus',
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
        );
      },
    );
  }
}

// ---------------------------------------------------------------------------
// Topic Tile
// ---------------------------------------------------------------------------

class _TopicTile extends StatefulWidget {
  const _TopicTile({
    required this.topic,
    required this.isActive,
    required this.isRenaming,
    required this.renamingTemp,
    required this.onTap,
    required this.onDelete,
    required this.onStartRename,
    required this.onConfirmRename,
    required this.onCancelRename,
    required this.onRenameChange,
  });

  final ChatTopic topic;
  final bool      isActive;
  final bool      isRenaming;
  final String?   renamingTemp;
  final VoidCallback onTap;
  final VoidCallback onDelete;
  final VoidCallback onStartRename;
  final void Function(String) onConfirmRename;
  final VoidCallback onCancelRename;
  final void Function(String) onRenameChange;

  @override
  State<_TopicTile> createState() => _TopicTileState();
}

class _TopicTileState extends State<_TopicTile> {
  bool _hovering = false;
  late TextEditingController _renameCtrl;

  @override
  void initState() {
    super.initState();
    _renameCtrl = TextEditingController(text: widget.topic.title);
  }

  @override
  void didUpdateWidget(_TopicTile old) {
    super.didUpdateWidget(old);
    if (widget.isRenaming && !old.isRenaming) {
      _renameCtrl.text = widget.topic.title;
      _renameCtrl.selection = TextSelection(
        baseOffset: 0, extentOffset: _renameCtrl.text.length,
      );
    }
  }

  @override
  void dispose() {
    _renameCtrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MouseRegion(
      onEnter: (_) => setState(() => _hovering = true),
      onExit:  (_) => setState(() => _hovering = false),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 150),
        margin: const EdgeInsets.only(bottom: 2),
        decoration: BoxDecoration(
          color: widget.isActive
              ? BatikColors.marun.withValues(alpha: 0.08)
              : _hovering
                  ? BatikColors.krem
                  : Colors.transparent,
          borderRadius: BorderRadius.circular(8),
          border: Border.all(
            color: widget.isActive
                ? BatikColors.marun.withValues(alpha: 0.3)
                : Colors.transparent,
          ),
        ),
        child: widget.isRenaming ? _buildRename() : _buildNormal(),
      ),
    );
  }

  Widget _buildNormal() {
    return InkWell(
      onTap: widget.onTap,
      borderRadius: BorderRadius.circular(8),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 9),
        child: Row(
          children: [
            Icon(
              Icons.chat_bubble_outline_rounded,
              size: 14,
              color: widget.isActive ? BatikColors.marun : BatikColors.abuTeks,
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                widget.topic.title,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: GoogleFonts.nunitoSans(
                  fontSize: 13,
                  color: widget.isActive
                      ? BatikColors.marun
                      : BatikColors.sogaGelap,
                  fontWeight: widget.isActive
                      ? FontWeight.w700
                      : FontWeight.w400,
                ),
              ),
            ),
            PopupMenuButton<String>(
              icon: const Icon(Icons.more_vert, size: 16, color: BatikColors.abuTeks),
              tooltip: "",
              offset: const Offset(0, 20),
              color: BatikColors.putih,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(8),
                side: const BorderSide(color: BatikColors.garis),
              ),
              onSelected: (value) {
                if (value == 'rename') {
                  widget.onStartRename();
                } else if (value == 'delete') {
                  widget.onDelete();
                }
              },
              itemBuilder: (context) => [
                PopupMenuItem<String>(
                  value: 'rename',
                  height: 36,
                  child: Row(
                    children: [
                      const Icon(Icons.drive_file_rename_outline_rounded,
                           size: 16, color: BatikColors.soga),
                      const SizedBox(width: 8),
                      Text(
                        'Ganti nama',
                        style: GoogleFonts.nunitoSans(
                          fontSize: 12,
                          color: BatikColors.sogaGelap,
                        ),
                      ),
                    ],
                  ),
                ),
                PopupMenuItem<String>(
                  value: 'delete',
                  height: 36,
                  child: Row(
                    children: [
                      const Icon(Icons.delete_outline_rounded,
                           size: 16, color: BatikColors.marun),
                      const SizedBox(width: 8),
                      Text(
                        'Hapus',
                        style: GoogleFonts.nunitoSans(
                          fontSize: 12,
                          color: BatikColors.sogaGelap,
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildRename() {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      child: Row(
        children: [
          const Icon(Icons.edit_rounded, size: 14, color: BatikColors.marun),
          const SizedBox(width: 8),
          Expanded(
            child: TextField(
              controller : _renameCtrl,
              autofocus  : true,
              style: GoogleFonts.nunitoSans(fontSize: 13, color: BatikColors.sogaGelap),
              cursorColor: BatikColors.marun,
              decoration : const InputDecoration(
                border: InputBorder.none,
                isDense: true,
                contentPadding: EdgeInsets.zero,
              ),
              onChanged  : widget.onRenameChange,
              onSubmitted: widget.onConfirmRename,
              inputFormatters: [LengthLimitingTextInputFormatter(60)],
            ),
          ),
          InkWell(
            onTap: () => widget.onConfirmRename(_renameCtrl.text),
            borderRadius: BorderRadius.circular(4),
            child: const Icon(Icons.check_rounded, size: 16, color: BatikColors.hijau),
          ),
          const SizedBox(width: 4),
          InkWell(
            onTap: widget.onCancelRename,
            borderRadius: BorderRadius.circular(4),
            child: const Icon(Icons.close_rounded,
                size: 16, color: BatikColors.abuTeks),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Profile Card
// ---------------------------------------------------------------------------

class _ProfileCard extends StatefulWidget {
  const _ProfileCard({required this.profile, required this.onTap});
  final ChatUserProfile? profile;
  final VoidCallback     onTap;

  @override
  State<_ProfileCard> createState() => _ProfileCardState();
}

class _ProfileCardState extends State<_ProfileCard> {
  bool _hovering = false;

  @override
  Widget build(BuildContext context) {
    final name     = widget.profile?.name  ?? '—';
    final email    = widget.profile?.email ?? '—';
    final initials = name.trim().isNotEmpty
        ? name.trim().split(' ').take(2).map((w) => w[0].toUpperCase()).join()
        : '?';

    return MouseRegion(
      onEnter: (_) => setState(() => _hovering = true),
      onExit:  (_) => setState(() => _hovering = false),
      child: GestureDetector(
        onTap: widget.onTap,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 150),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
          decoration: BoxDecoration(
            color: _hovering
                ? BatikColors.marun.withValues(alpha: 0.06)
                : BatikColors.krem,
            borderRadius: BorderRadius.circular(14),
            border: Border.all(
              color: _hovering
                  ? BatikColors.marun.withValues(alpha: 0.4)
                  : BatikColors.garis,
            ),
          ),
          child: Row(
            children: [
              Container(
                width: 36, height: 36,
                decoration: BoxDecoration(
                  shape : BoxShape.circle,
                  color : BatikColors.marun.withValues(alpha: 0.12),
                  border: Border.all(color: BatikColors.marun.withValues(alpha: 0.5)),
                ),
                alignment: Alignment.center,
                child: Text(
                  initials,
                  style: GoogleFonts.nunitoSans(
                    fontSize: 13,
                    fontWeight: FontWeight.w700,
                    color: BatikColors.marun,
                  ),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      name,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: GoogleFonts.nunitoSans(
                        fontSize: 13,
                        fontWeight: FontWeight.w700,
                        color: BatikColors.sogaGelap,
                      ),
                    ),
                    Text(
                      email,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: GoogleFonts.nunitoSans(
                          fontSize: 11, color: BatikColors.abuTeks),
                    ),
                  ],
                ),
              ),
              const Icon(Icons.chevron_right_rounded,
                  size: 18, color: BatikColors.abuTeks),
            ],
          ),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Logout Button
// ---------------------------------------------------------------------------

class _LogoutButton extends StatelessWidget {
  const _LogoutButton({required this.onPressed});
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: double.infinity,
      height: 42,
      child: OutlinedButton.icon(
        onPressed: onPressed,
        icon : const Icon(Icons.logout_rounded,
            size: 16, color: BatikColors.marun),
        label: Text(
          'Keluar',
          style: GoogleFonts.nunitoSans(
            fontSize: 13,
            fontWeight: FontWeight.w600,
            color: BatikColors.marun,
          ),
        ),
        style: OutlinedButton.styleFrom(
          side: const BorderSide(color: BatikColors.marun, width: 1),
          shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(10)),
          backgroundColor: BatikColors.marun.withValues(alpha: 0.06),
        ),
      ),
    );
  }
}